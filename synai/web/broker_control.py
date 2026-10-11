from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from synai.broker.protocol import BrokerError, TaskClaim
from synai.web.app import WebServices
from synai.web.broker_authority import CoreBrokerAuthority, MAX_RESULT_BYTES
from synai.web.distributed import DistributedError


@dataclass(frozen=True)
class BrokerControlConfig:
    enabled: bool
    port: int
    broker_ca_file: Path
    core_cert_file: Path
    core_key_file: Path
    signing_key: bytes
    deployment_id: str
    integration_test_token: str | None = None

    @classmethod
    def from_env(cls) -> BrokerControlConfig | None:
        if os.environ.get("SYNAI_CORE_BROKER_CONTROL") != "1":
            return None
        required = (
            "SYNAI_CORE_BROKER_CA", "SYNAI_CORE_BROKER_CERT",
            "SYNAI_CORE_BROKER_KEY", "SYNAI_CORE_BROKER_SIGNING_KEY",
            "SYNAI_CORE_BROKER_DEPLOYMENT_ID",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError("Core Broker control configuration is incomplete: " + ", ".join(missing))
        try:
            signing_key = bytes.fromhex(os.environ["SYNAI_CORE_BROKER_SIGNING_KEY"])
        except ValueError as exc:
            raise ValueError("Core Broker signing key must be hexadecimal") from exc
        selected = cls(
            enabled=True,
            port=int(os.environ.get("SYNAI_CORE_BROKER_PORT", "8767")),
            broker_ca_file=Path(os.environ["SYNAI_CORE_BROKER_CA"]),
            core_cert_file=Path(os.environ["SYNAI_CORE_BROKER_CERT"]),
            core_key_file=Path(os.environ["SYNAI_CORE_BROKER_KEY"]),
            signing_key=signing_key,
            deployment_id=os.environ["SYNAI_CORE_BROKER_DEPLOYMENT_ID"],
            integration_test_token=os.environ.get("SYNAI_CORE_BROKER_TEST_TOKEN")
            if os.environ.get("SYNAI_CORE_BROKER_TEST_MODE") == "1" else None,
        )
        if (
            not 1 <= selected.port <= 65535 or len(signing_key) != 32
            or len(selected.deployment_id) != 32
            or any(ch not in "0123456789abcdef" for ch in selected.deployment_id)
        ):
            raise ValueError("Core Broker control configuration is invalid")
        for path, private in (
            (selected.broker_ca_file, False),
            (selected.core_cert_file, False),
            (selected.core_key_file, True),
        ):
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_size == 0
                or private and (info.st_uid != os.geteuid() or info.st_mode & 0o077)
            ):
                raise ValueError("Core Broker mTLS credential permissions are unsafe")
        return selected


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: dict[str, Any] = Field(max_length=32)
    signature: str = Field(min_length=128, max_length=128, pattern=r"^[a-f0-9]{128}$")


class SnapshotRequest(ClaimRequest):
    grant_token: str = Field(min_length=32, max_length=128)


class ResultRequest(ClaimRequest):
    envelope: dict[str, Any] = Field(max_length=64)


class ClaimOutcomeRequest(ClaimRequest):
    state: Literal["failed", "uncertain"]


class RequestSizeLimitMiddleware:
    def __init__(self, app: Any, maximum: int) -> None:
        self.app = app
        self.maximum = maximum

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = next(
            (value.decode("ascii", errors="ignore") for key, value in scope["headers"] if key == b"content-length"),
            None,
        )
        if length is not None:
            try:
                if int(length) < 0 or int(length) > self.maximum:
                    raise ValueError
            except ValueError:
                await JSONResponse(
                    status_code=413,
                    content={"error": {"code": "request_too_large", "message": "Core Broker request exceeds its size limit."}},
                )(scope, receive, send)
                return
        chunks: list[dict[str, Any]] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            size += len(message.get("body", b""))
            if size > self.maximum:
                await JSONResponse(
                    status_code=413,
                    content={"error": {"code": "request_too_large", "message": "Core Broker request exceeds its size limit."}},
                )(scope, receive, send)
                return
            chunks.append(message)
            if not message.get("more_body", False):
                break
        position = 0

        async def replay() -> dict[str, Any]:
            nonlocal position
            if position < len(chunks):
                message = chunks[position]
                position += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)


def create_broker_control_app(
    web_app: FastAPI,
    config: BrokerControlConfig,
) -> FastAPI:
    app = FastAPI(
        title="SynAI Core Broker Control", version="1",
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.add_middleware(RequestSizeLimitMiddleware, maximum=10 * 1024 * 1024)

    def authority() -> tuple[CoreBrokerAuthority, WebServices]:
        services: WebServices = web_app.state.services
        if not services.ready or services.snapshots is None:
            raise DistributedError("core_not_ready", "Core task authority is not ready.", 503)
        cached = getattr(app.state, "authority", None)
        if cached is None:
            cached = CoreBrokerAuthority(
                services.database, services.snapshots, config.signing_key,
                deployment_id=config.deployment_id,
                integration_test_token=config.integration_test_token,
            )
            app.state.authority = cached
        return cached, services

    @app.exception_handler(DistributedError)
    async def distributed_error(_: Request, exc: DistributedError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.public_message}},
        )

    @app.exception_handler(BrokerError)
    async def broker_error(_: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.public_message}},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "request_invalid", "message": "Core Broker control request is invalid."}},
        )

    @app.middleware("http")
    async def require_tls(request: Request, call_next: Any) -> Any:
        if request.scope.get("scheme") != "https":
            return JSONResponse(
                status_code=426,
                content={"error": {"code": "mtls_required", "message": "Core Broker control requires mutually authenticated TLS."}},
            )
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.post("/v1/claims/validate")
    async def validate_claim(body: ClaimRequest) -> dict[str, bool]:
        service, _ = authority()
        claim = TaskClaim.from_dict(body.claim)
        return {"authorized": service.validate_live_claim(claim, body.signature)}

    @app.post("/v1/claims/running")
    async def mark_running(body: ClaimRequest) -> dict[str, str]:
        service, _ = authority()
        claim = TaskClaim.from_dict(body.claim)
        service.mark_operation_running(claim, body.signature)
        return {"state": "running"}

    @app.post("/v1/claims/outcome")
    async def record_outcome(body: ClaimOutcomeRequest) -> dict[str, str]:
        service, _ = authority()
        claim = TaskClaim.from_dict(body.claim)
        service.record_operation_state(claim, body.signature, body.state)
        return {"state": body.state}

    @app.post("/v1/snapshots/retrieve")
    async def retrieve_snapshot(body: SnapshotRequest) -> dict[str, Any]:
        service, _ = authority()
        claim = TaskClaim.from_dict(body.claim)
        manifest, files = service.fetch_exact_snapshot(
            claim, body.signature, body.grant_token,
        )
        import base64
        return {
            "manifest": manifest,
            "files": {path: base64.b64encode(data).decode("ascii") for path, data in files.items()},
        }

    @app.post("/v1/results")
    async def record_result(body: ResultRequest) -> dict[str, Any]:
        if len(body.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
            raise DistributedError("broker_result_invalid", "Broker result exceeds its Core limit.", 413)
        service, _ = authority()
        claim = TaskClaim.from_dict(body.claim)
        return service.validate_and_record_result(claim, body.signature, body.envelope)

    app.state.web_app = web_app
    app.state.config = config
    return app
