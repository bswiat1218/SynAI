from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from synai.broker.config import BrokerConfig
from synai.broker.operations import execute_file_operation, validate_claim_operation
from synai.broker.protocol import (
    MAX_REQUEST_BYTES,
    BrokerError,
    TaskClaim,
    canonical_json,
    request_mac,
    validate_nonce,
    verify_claim,
)
from synai.broker.registry import BrokerRegistry
from synai.broker.runtime import RootlessPodmanRuntime
from synai.broker.snapshots import (
    changed_files,
    load_staged_manifest,
    stage_snapshot,
    validate_snapshot,
)


class SnapshotRetriever(Protocol):
    async def fetch_exact(
        self, *, task_id: str, project_id: str, snapshot_id: str,
        snapshot_digest: str, deadline: int,
    ) -> tuple[dict[str, Any], dict[str, bytes]]: ...


LeaseValidator = Callable[[TaskClaim], Awaitable[bool]]


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    claim: dict[str, Any] = Field(max_length=32)
    claim_signature: str = Field(min_length=128, max_length=128, pattern=r"^[a-f0-9]{128}$")
    kind: str = Field(min_length=1, max_length=32)
    arguments: dict[str, Any] = Field(max_length=32)


class BrokerService:
    def __init__(
        self,
        config: BrokerConfig,
        *,
        snapshot_retriever: SnapshotRetriever | None = None,
        lease_validator: LeaseValidator | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.registry = BrokerRegistry(config)
        self.runtime = RootlessPodmanRuntime(config, self.registry)
        self.snapshot_retriever = snapshot_retriever
        self.lease_validator = lease_validator
        self.readiness = None
        self._task_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        self.registry.initialize()
        await self.recover()
        self.readiness = await self.runtime.verify_startup()

    async def recover(self) -> None:
        # Registry initialization marks uncertain operations and leaves every
        # interrupted container quarantined. No command is replayed and no
        # runtime-wide cleanup is performed.
        return

    async def dispatch(self, payload: OperationRequest) -> dict[str, Any]:
        try:
            claim = TaskClaim.from_dict(payload.claim)
            verify_claim(claim, payload.claim_signature, self.config.claim_key)
        except (BrokerError, ValueError) as exc:
            raise
        if payload.kind != claim.tool_kind:
            raise BrokerError("operation_claim_mismatch", "Operation kind differs from the signed task claim.", 403)
        arguments = validate_claim_operation(claim.tool_kind, payload.kind, payload.arguments)
        argument_digest = hashlib.sha256(canonical_json(arguments)).hexdigest()
        if not hmac.compare_digest(argument_digest, claim.arguments_digest):
            raise BrokerError("operation_arguments_mismatch", "Operation arguments differ from the signed task claim.", 403)
        if self.lease_validator is None:
            raise BrokerError("lease_authority_unavailable", "Current task lease authority is unavailable.", 503)
        try:
            lease_timeout = min(
                self.config.profile.operation_seconds,
                max(0, claim.deadline - time.time()),
            )
            current_lease = await asyncio.wait_for(
                self.lease_validator(claim),
                timeout=lease_timeout,
            )
        except TimeoutError as exc:
            raise BrokerError("claim_invalid", "Task claim expired during lease validation.", 403) from exc
        except OSError as exc:
            raise BrokerError("lease_authority_unavailable", "Current task lease authority is unavailable.", 503) from exc
        if current_lease is not True:
            raise BrokerError("stale_fence", "Task lease is stale or no longer authorized.", 409)
        self.registry.authorize(claim, payload.claim_signature)
        try:
            remaining = min(
                self.config.profile.operation_seconds,
                claim.deadline - time.time(),
            )
            if remaining <= 0:
                raise BrokerError("claim_invalid", "Task claim expired before operation dispatch.", 403)

            async def run_operation() -> dict[str, Any]:
                if claim.tool_kind in {"cancel", "terminate", "remove"}:
                    result = await self._execute(claim, arguments)
                else:
                    lock = self._task_locks.setdefault(claim.task_id, asyncio.Lock())
                    async with lock:
                        result = await self._execute(claim, arguments)
                return result

            result = await asyncio.wait_for(run_operation(), timeout=remaining)
            encoded = canonical_json(result)
            if len(encoded) > self.config.profile.output_bytes:
                raise BrokerError("operation_output_limit", "Task operation result exceeds its configured limit.", 413)
            self.registry.complete_operation(claim.operation_id, "completed", encoded.decode("utf-8"))
            return {
                "schema_version": 1, "operation_id": claim.operation_id,
                "task_id": claim.task_id, "status": "completed", "result": result,
            }
        except BrokerError:
            self.registry.complete_operation(claim.operation_id, "failed")
            if claim.tool_kind == "prepare":
                self._finish_incomplete_preparation(claim.task_id, "failed")
            raise
        except (OSError, TimeoutError, asyncio.CancelledError):
            self.registry.complete_operation(claim.operation_id, "uncertain")
            if claim.tool_kind == "prepare":
                self._finish_incomplete_preparation(claim.task_id, "uncertain")
            raise

    def _finish_incomplete_preparation(self, task_id: str, state: Literal["failed", "uncertain"]) -> None:
        task = self.registry.task(task_id)
        if task["state"] == "preparing":
            self.registry.transition(task_id, state)

    async def _execute(self, claim: TaskClaim, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = claim.tool_kind
        if kind == "prepare":
            if self.snapshot_retriever is None:
                raise BrokerError("snapshot_source_unavailable", "Exact Core snapshot retrieval is not configured.", 503)
            manifest, files = await self.snapshot_retriever.fetch_exact(
                task_id=claim.task_id, project_id=claim.project_id,
                snapshot_id=claim.snapshot_id, snapshot_digest=claim.snapshot_digest,
                deadline=claim.deadline,
            )
            validate_snapshot(manifest, claim.snapshot_id, claim.snapshot_digest, files)
            if manifest["project_id"] != claim.project_id:
                raise BrokerError("snapshot_project_mismatch", "Snapshot belongs to another logical project.", 403)
            stage_snapshot(self.config.state_dir, claim.task_id, manifest, files)
            self.registry.transition(claim.task_id, "ready")
            return {
                "state": "ready", "snapshot_id": claim.snapshot_id,
                "snapshot_digest": claim.snapshot_digest, "workspace_prepared": True,
            }
        if kind == "start":
            if self.readiness is None or not self.readiness.execution_available:
                raise BrokerError("execution_unavailable", "Rootless task execution has not passed Broker readiness checks.", 503)
            allocation_id = secrets.token_hex(16)
            self.registry.reserve_container(claim.task_id, allocation_id)
            workspace = self.config.state_dir / "tasks" / claim.task_id / "workspace"
            try:
                container_id = await self.runtime.create_task(claim.task_id, allocation_id, workspace)
                self.registry.bind_container(claim.task_id, allocation_id, container_id)
                await self.runtime.start_owned(claim.task_id)
                self.registry.transition(claim.task_id, "running")
            except BaseException:
                self.registry.mark_container(claim.task_id, "quarantined")
                self.registry.transition(claim.task_id, "uncertain")
                raise
            return {"state": "running", "allocation_id": allocation_id}
        if kind == "status":
            task = self.registry.task(claim.task_id)
            info: dict[str, Any] | None = None
            try:
                owned = self.registry.owned_container(claim.task_id)
            except BrokerError as exc:
                if exc.code != "container_quarantined":
                    raise
            else:
                try:
                    info = await self.runtime.inspect_owned(claim.task_id)
                except BrokerError:
                    self.registry.mark_container(claim.task_id, "quarantined")
                    if task["state"] in {"running", "cancelling"}:
                        self.registry.transition(claim.task_id, "uncertain")
                    raise
                if info["State"]["Running"] is not True and task["state"] == "running":
                    self.registry.transition(claim.task_id, "interrupted")
                    task = self.registry.task(claim.task_id)
            return {
                "state": task["state"], "container_state": None if info is None else info["State"]["Status"],
                "snapshot_digest": task["snapshot_digest"],
            }
        if kind in {"read_file", "list_files", "write_file", "patch_file", "delete_file"}:
            task = self.registry.task(claim.task_id)
            if task["state"] not in {"ready", "running"}:
                raise BrokerError("task_state_invalid", "Task workspace is not available for an operation.", 409)
            workspace = self.config.state_dir / "tasks" / claim.task_id / "workspace"
            result = execute_file_operation(workspace, kind, arguments, self.config.profile.output_bytes)
            return {**result, "duration_seconds": 0, "exit_status": 0}
        if kind in {"command", "verify"}:
            if self.readiness is None or not self.readiness.execution_available:
                raise BrokerError("execution_unavailable", "Rootless task execution has not passed Broker readiness checks.", 503)
            task = self.registry.task(claim.task_id)
            if task["state"] != "running":
                raise BrokerError("task_state_invalid", "Task container is not running.", 409)
            result = await self.runtime.execute(
                claim.task_id, arguments["argv"], arguments["cwd"],
                self.config.profile.operation_seconds, self.config.profile.output_bytes,
            )
            result["operation_id"] = claim.operation_id
            result["input_snapshot_digest"] = claim.snapshot_digest
            return result
        if kind == "collect":
            task = self.registry.task(claim.task_id)
            workspace = self.config.state_dir / "tasks" / claim.task_id / "workspace"
            manifest = load_staged_manifest(
                self.config.state_dir, claim.task_id, task["snapshot_digest"],
            )
            changes = changed_files(workspace, manifest, self.config.profile.workspace_bytes)
            artifact_digest = hashlib.sha256(canonical_json(changes)).hexdigest()
            return {
                "input_snapshot_digest": task["snapshot_digest"],
                "changed_files": changes, "artifact_manifest_sha256": artifact_digest,
                "collection_complete": True,
            }
        if kind == "complete":
            task = self.registry.task(claim.task_id)
            if task["state"] != "running":
                raise BrokerError("task_state_invalid", "Only a running task can be completed.", 409)
            evidence = await self.runtime.terminate_owned(claim.task_id)
            self.registry.transition(claim.task_id, "completed")
            return {"state": "completed", "termination_evidence": evidence}
        if kind in {"cancel", "terminate", "remove"}:
            task = self.registry.task(claim.task_id)
            if task["state"] == "removed" and kind == "remove":
                return {"state": "removed", "idempotent": True}
            if task["state"] == "running":
                self.registry.transition(claim.task_id, "cancelling")
            elif task["state"] not in {"ready", "cancelling", "cleaning_up", "completed", "failed"}:
                raise BrokerError("task_state_invalid", "Task cannot be terminated in its current state.", 409)
            if task["state"] != "cleaning_up":
                self.registry.transition(claim.task_id, "cleaning_up")
            try:
                container = self.registry.container_record(claim.task_id)
                if container is None:
                    task_root = self.config.state_dir / "tasks" / claim.task_id
                    _remove_task_tree(task_root)
                    self.registry.transition(claim.task_id, "removed")
                    return {"state": "removed", "cancelled_before_start": True}
                if container["lifecycle_state"] not in {"owned", "terminated"}:
                    raise BrokerError("container_quarantined", "Task container allocation is uncertain and cannot be cleaned automatically.", 409)
                evidence = await self.runtime.terminate_owned(claim.task_id)
                await self.runtime.wait_cancelled_operation(claim.task_id)
                if kind == "remove":
                    cleanup = await self.runtime.remove_owned(claim.task_id)
                    self.registry.transition(claim.task_id, "removed")
                    return {"state": "removed", "termination_evidence": evidence, "cleanup_evidence": cleanup}
                return {"state": "cleaning_up", "termination_evidence": evidence}
            except BrokerError:
                if self.registry.task(claim.task_id)["state"] == "cleaning_up":
                    self.registry.transition(claim.task_id, "uncertain")
                raise
        raise BrokerError("tool_not_allowed", "Operation kind is not supported.", 403)


def create_app(service: BrokerService) -> FastAPI:
    app = FastAPI(title="SynAI Sandbox Broker", version="1")
    app.state.broker = service

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
            content={"error": {"code": "request_invalid", "message": "Broker request does not match protocol v1."}},
        )

    async def authenticate(
        request: Request,
        timestamp: str | None = Header(default=None, alias="X-SynAI-Broker-Timestamp"),
        nonce: str | None = Header(default=None, alias="X-SynAI-Broker-Nonce"),
        signature: str | None = Header(default=None, alias="X-SynAI-Broker-Signature"),
    ) -> None:
        if not timestamp or not nonce or not signature:
            raise BrokerError("request_auth_invalid", "Broker request authentication is required.", 401)
        try:
            issued = int(timestamp)
            validate_nonce(nonce, issued)
            body = await request.body()
            expected = request_mac(request.method, request.url.path, issued, nonce, body, service.config.service_key)
        except (ValueError, UnicodeError) as exc:
            raise BrokerError("request_auth_invalid", "Broker request authentication is invalid.", 401) from exc
        if not hmac.compare_digest(expected, signature):
            raise BrokerError("request_auth_invalid", "Broker request authentication is invalid.", 401)
        service.registry.consume_request_nonce(nonce, int(time.time()))

    app.add_middleware(RequestSizeLimitMiddleware, maximum=MAX_REQUEST_BYTES)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/v1/ready", dependencies=[Depends(authenticate)])
    async def readiness() -> dict[str, Any]:
        state = service.readiness
        if state is None:
            return {"schema_version": 1, "ready": False, "execution_available": False, "reasons": ["startup_verification_incomplete"]}
        return {
            "schema_version": 1, "ready": state.ready,
            "execution_available": state.execution_available,
            "checks": state.checks, "reasons": list(state.reasons),
        }

    @app.post("/v1/tasks/{task_id}/operations")
    async def operation(
        task_id: str,
        payload: OperationRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        claim = TaskClaim.from_dict(payload.claim)
        if task_id != claim.task_id:
            raise BrokerError("task_binding_mismatch", "URL task identity differs from the signed task claim.", 403)
        return await service.dispatch(payload)

    return app


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
                    content={"error": {"code": "request_too_large", "message": "Broker request exceeds its size limit."}},
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
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.maximum:
                await JSONResponse(
                    status_code=413,
                    content={"error": {"code": "request_too_large", "message": "Broker request exceeds its size limit."}},
                )(scope, receive, send)
                return
            chunks.append(message)
            if not message.get("more_body", False):
                break
        cursor = 0

        async def replay() -> dict[str, Any]:
            nonlocal cursor
            if cursor < len(chunks):
                message = chunks[cursor]
                cursor += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)


def _remove_task_tree(task_root: Path) -> None:
    try:
        info = task_root.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        raise BrokerError("workspace_cleanup_refused", "Task workspace ownership cannot be verified.", 409)
    for current, dirs, files in os.walk(task_root, topdown=True, followlinks=False):
        os.chmod(current, 0o700)
        for name in (*dirs, *files):
            path = Path(current) / name
            item = path.lstat()
            if stat.S_ISLNK(item.st_mode) or item.st_uid != os.geteuid():
                raise BrokerError("workspace_cleanup_refused", "Task workspace contains an untrusted filesystem entry.", 409)
    import shutil
    shutil.rmtree(task_root)
