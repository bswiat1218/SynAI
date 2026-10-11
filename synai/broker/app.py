from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import FastAPI, Request
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
        self, *, claim: TaskClaim, signature: str, grant_token: str,
    ) -> tuple[dict[str, Any], dict[str, bytes]]: ...


LeaseValidator = Callable[[TaskClaim, str], Awaitable[bool]]
OperationStarter = Callable[[TaskClaim, str], Awaitable[None]]
ResultRecorder = Callable[[TaskClaim, str, dict[str, Any]], Awaitable[dict[str, Any]]]
OperationOutcomeRecorder = Callable[[TaskClaim, str, str], Awaitable[None]]
SideEffectMarker = Callable[[], None]


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    claim: dict[str, Any] = Field(max_length=32)
    claim_signature: str = Field(min_length=128, max_length=128, pattern=r"^[a-f0-9]{128}$")
    kind: str = Field(min_length=1, max_length=32)
    arguments: dict[str, Any] = Field(max_length=32)
    snapshot_grant: str | None = Field(default=None, min_length=32, max_length=128)


class BrokerService:
    def __init__(
        self,
        config: BrokerConfig,
        *,
        snapshot_retriever: SnapshotRetriever | None = None,
        lease_validator: LeaseValidator | None = None,
        operation_starter: OperationStarter | None = None,
        result_recorder: ResultRecorder | None = None,
        operation_outcome_recorder: OperationOutcomeRecorder | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.registry = BrokerRegistry(config)
        self.runtime = RootlessPodmanRuntime(config, self.registry)
        self.core_client = None
        if config.readiness().checks.get("mtls_material") is True:
            from synai.broker.core_client import CoreBrokerClient
            self.core_client = CoreBrokerClient(config)
        self.snapshot_retriever = snapshot_retriever or self.core_client
        self.lease_validator = lease_validator or (
            None if self.core_client is None else self.core_client.validate_live_claim
        )
        self.operation_starter = operation_starter or (
            None if self.core_client is None else self.core_client.mark_operation_running
        )
        self.result_recorder = result_recorder or (
            None if self.core_client is None else self.core_client.record_result
        )
        self.operation_outcome_recorder = operation_outcome_recorder or (
            None if self.core_client is None else self.core_client.record_operation_state
        )
        self.readiness = None
        self._task_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        self.registry.initialize()
        self.readiness = await self.runtime.verify_startup()
        await self.recover()

    async def close(self) -> None:
        if self.core_client is not None:
            await self.core_client.close()

    async def recover(self) -> None:
        if self.operation_outcome_recorder is not None:
            for operation in self.registry.uncertain_operations():
                if not operation["claim_json"] or not operation["claim_signature"]:
                    continue
                claim = TaskClaim.from_dict(json.loads(operation["claim_json"]))
                try:
                    await self.operation_outcome_recorder(
                        claim, operation["claim_signature"], "uncertain",
                    )
                except BrokerError:
                    # The local uncertainty is durable; a later startup may
                    # retry recording it, but the operation itself is never replayed.
                    continue
        for record in self.registry.container_records():
            task_id = record["task_id"]
            if not record["container_id"]:
                self.registry.record_reconciliation(
                    task_id, "quarantined",
                    canonical_json({
                        "state": "ambiguous_allocation", "observed_at": int(time.time()),
                        "reason": "No runtime container ID was durably recorded.",
                    }).decode(),
                )
                continue
            try:
                info = await self.runtime.inspect_recorded(task_id)
                if info["State"]["Running"] is True:
                    await self.runtime.terminate_recorded(task_id)
                    info = await self.runtime.inspect_recorded(task_id)
                state = "terminated" if info["State"]["Running"] is False else "quarantined"
                evidence = canonical_json({
                    "state": "verified_stopped" if state == "terminated" else "running_unresolved",
                    "container_id": record["container_id"],
                    "image_digest": record["image_digest"],
                    "snapshot_digest": record["snapshot_digest"],
                    "observed_at": int(time.time()),
                }).decode()
                self.registry.record_reconciliation(task_id, state, evidence)
            except (BrokerError, OSError, TimeoutError) as exc:
                self.registry.record_reconciliation(
                    task_id, "quarantined",
                    canonical_json({
                        "state": "unverified", "container_id": record["container_id"],
                        "error_code": exc.code if isinstance(exc, BrokerError) else "runtime_unavailable",
                        "observed_at": int(time.time()),
                    }).decode(),
                )

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
                self.lease_validator(claim, payload.claim_signature),
                timeout=lease_timeout,
            )
        except TimeoutError as exc:
            raise BrokerError("claim_invalid", "Task claim expired during lease validation.", 403) from exc
        except OSError as exc:
            raise BrokerError("lease_authority_unavailable", "Current task lease authority is unavailable.", 503) from exc
        if current_lease is not True:
            raise BrokerError("stale_fence", "Task lease is stale or no longer authorized.", 409)
        self.registry.authorize(claim, payload.claim_signature)
        side_effects_started = False
        try:
            remaining = min(
                self.config.profile.operation_seconds,
                claim.deadline - time.time(),
            )
            if remaining <= 0:
                raise BrokerError("claim_invalid", "Task claim expired before operation dispatch.", 403)

            async def run_operation() -> dict[str, Any]:
                nonlocal side_effects_started
                def mark_side_effect() -> None:
                    nonlocal side_effects_started
                    side_effects_started = True

                if claim.tool_kind in {"cancel", "terminate", "remove"}:
                    if self.operation_starter is not None:
                        await self.operation_starter(claim, payload.claim_signature)
                    if self.lease_validator is None or not await self.lease_validator(
                        claim, payload.claim_signature,
                    ):
                        raise BrokerError("stale_fence", "Task authority was revoked before lifecycle dispatch.", 409)
                    self.registry.begin_operation(claim.operation_id)
                    result = await self._execute(
                        claim, arguments, payload.claim_signature, payload.snapshot_grant,
                        mark_side_effect,
                    )
                else:
                    lock = self._task_locks.setdefault(claim.task_id, asyncio.Lock())
                    async with lock:
                        if self.lease_validator is None or not await self.lease_validator(
                            claim, payload.claim_signature,
                        ):
                            raise BrokerError("stale_fence", "Task authority was revoked before operation dispatch.", 409)
                        if self.operation_starter is not None:
                            await self.operation_starter(claim, payload.claim_signature)
                        if self.lease_validator is None or not await self.lease_validator(
                            claim, payload.claim_signature,
                        ):
                            raise BrokerError("stale_fence", "Task authority was revoked before operation dispatch.", 409)
                        self.registry.begin_operation(claim.operation_id)
                        result = await self._execute_with_lease_monitor(
                            claim, payload.claim_signature, arguments, payload.snapshot_grant,
                            mark_side_effect,
                        )
                return result

            result = await asyncio.wait_for(run_operation(), timeout=remaining)
            result_envelope = {
                "schema_version": 1, "claim_id": claim.claim_id,
                "operation_id": claim.operation_id, "task_id": claim.task_id,
                "project_id": claim.project_id, "snapshot_id": claim.snapshot_id,
                "snapshot_digest": claim.snapshot_digest,
                "allocation_id": claim.allocation_id,
                "lease_generation": claim.lease_generation,
                "status": "completed", "result": result,
            }
            encoded = canonical_json(result_envelope)
            if len(encoded) > self.config.profile.output_bytes:
                raise BrokerError("operation_output_limit", "Task operation result exceeds its configured limit.", 413)
            if self.result_recorder is not None:
                await self.result_recorder(claim, payload.claim_signature, result_envelope)
            self.registry.complete_operation(claim.operation_id, "completed", encoded.decode("utf-8"))
            return {
                "schema_version": 1, "operation_id": claim.operation_id,
                "task_id": claim.task_id, "status": "completed", "result": result,
            }
        except BrokerError:
            terminal = "uncertain" if side_effects_started else "failed_before_effects"
            self.registry.complete_operation(
                claim.operation_id, terminal,
            )
            if claim.tool_kind == "prepare":
                self._finish_incomplete_preparation(
                    claim.task_id, "uncertain" if side_effects_started else "failed",
                )
            if self.operation_outcome_recorder is not None:
                await self.operation_outcome_recorder(
                    claim, payload.claim_signature,
                    "uncertain" if side_effects_started else "failed",
                )
            raise
        except (OSError, TimeoutError, asyncio.CancelledError):
            terminal = "uncertain" if side_effects_started else "failed_before_effects"
            self.registry.complete_operation(claim.operation_id, terminal)
            if claim.tool_kind == "prepare":
                self._finish_incomplete_preparation(
                    claim.task_id, "uncertain" if side_effects_started else "failed",
                )
            if self.operation_outcome_recorder is not None:
                await self.operation_outcome_recorder(
                    claim, payload.claim_signature,
                    "uncertain" if side_effects_started else "failed",
                )
            raise
        except Exception:
            terminal = "uncertain" if side_effects_started else "failed_before_effects"
            self.registry.complete_operation(claim.operation_id, terminal)
            if claim.tool_kind == "prepare":
                self._finish_incomplete_preparation(
                    claim.task_id, "uncertain" if side_effects_started else "failed",
                )
            if self.operation_outcome_recorder is not None:
                await self.operation_outcome_recorder(
                    claim, payload.claim_signature,
                    "uncertain" if side_effects_started else "failed",
                )
            raise

    async def _execute_with_lease_monitor(
        self,
        claim: TaskClaim,
        signature: str,
        arguments: dict[str, Any],
        snapshot_grant: str | None,
        mark_side_effect: SideEffectMarker,
    ) -> dict[str, Any]:
        assert self.lease_validator is not None
        operation = asyncio.create_task(
            self._execute(claim, arguments, signature, snapshot_grant, mark_side_effect),
        )
        try:
            while True:
                done, _ = await asyncio.wait({operation}, timeout=2)
                if done:
                    return operation.result()
                try:
                    valid = await asyncio.wait_for(
                        self.lease_validator(claim, signature),
                        timeout=min(3, max(0.1, claim.deadline - time.time())),
                    )
                except (OSError, TimeoutError) as exc:
                    valid = False
                    reason = exc
                else:
                    reason = None
                if valid is not True:
                    try:
                        await self.runtime.terminate_owned(claim.task_id)
                    except BrokerError:
                        self.registry.mark_container(claim.task_id, "quarantined")
                    operation.cancel()
                    try:
                        await operation
                    except (asyncio.CancelledError, BrokerError):
                        pass
                    if reason is not None:
                        raise BrokerError("lease_authority_unavailable", "Core lease authority was lost during task execution.", 503) from reason
                    raise BrokerError("stale_fence", "Task authorization was revoked during execution.", 409)
        except asyncio.CancelledError:
            if not operation.done():
                try:
                    await self.runtime.terminate_owned(claim.task_id)
                except BrokerError:
                    self.registry.mark_container(claim.task_id, "quarantined")
                operation.cancel()
                try:
                    await operation
                except (asyncio.CancelledError, BrokerError):
                    pass
            raise

    def _finish_incomplete_preparation(self, task_id: str, state: Literal["failed", "uncertain"]) -> None:
        task = self.registry.task(task_id)
        if task["state"] == "preparing":
            self.registry.transition(task_id, state)

    async def _execute(
        self,
        claim: TaskClaim,
        arguments: dict[str, Any],
        signature: str,
        snapshot_grant: str | None,
        mark_side_effect: SideEffectMarker,
    ) -> dict[str, Any]:
        kind = claim.tool_kind
        if kind == "prepare":
            if self.snapshot_retriever is None:
                raise BrokerError("snapshot_source_unavailable", "Exact Core snapshot retrieval is not configured.", 503)
            if self.core_client is not None and snapshot_grant is None:
                raise BrokerError("snapshot_grant_missing", "Exact Core snapshot retrieval grant is required.", 403)
            manifest, files = await self.snapshot_retriever.fetch_exact(
                claim=claim, signature=signature,
                grant_token=snapshot_grant or "",
            )
            validate_snapshot(manifest, claim.snapshot_id, claim.snapshot_digest, files)
            if manifest["project_id"] != claim.project_id:
                raise BrokerError("snapshot_project_mismatch", "Snapshot belongs to another logical project.", 403)
            mark_side_effect()
            stage_snapshot(self.config.state_dir, claim.task_id, manifest, files)
            self.registry.transition(claim.task_id, "ready")
            return {
                "state": "ready", "snapshot_id": claim.snapshot_id,
                "snapshot_digest": claim.snapshot_digest, "workspace_prepared": True,
            }
        if kind == "start":
            if self.readiness is None or not self.readiness.execution_available:
                raise BrokerError("execution_unavailable", "Rootless task execution has not passed Broker readiness checks.", 503)
            allocation_id = claim.allocation_id
            mark_side_effect()
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
            if kind in {"write_file", "patch_file", "delete_file"}:
                mark_side_effect()
            result = execute_file_operation(workspace, kind, arguments, self.config.profile.output_bytes)
            return {**result, "duration_seconds": 0, "exit_status": 0}
        if kind in {"command", "verify"}:
            if self.readiness is None or not self.readiness.execution_available:
                raise BrokerError("execution_unavailable", "Rootless task execution has not passed Broker readiness checks.", 503)
            task = self.registry.task(claim.task_id)
            if task["state"] != "running":
                raise BrokerError("task_state_invalid", "Task container is not running.", 409)
            mark_side_effect()
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
            changes = changed_files(
                workspace, manifest, self.config.profile.workspace_bytes,
                self.config.profile.output_bytes,
            )
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
            mark_side_effect()
            evidence = await self.runtime.terminate_owned(claim.task_id)
            self.registry.transition(claim.task_id, "completed")
            return {"state": "completed", "termination_evidence": evidence}
        if kind in {"cancel", "terminate", "remove"}:
            task = self.registry.task(claim.task_id)
            if task["state"] == "removed" and kind == "remove":
                return {"state": "removed", "idempotent": True}
            if task["state"] == "running":
                mark_side_effect()
                self.registry.transition(claim.task_id, "cancelling")
            elif task["state"] not in {"ready", "cancelling", "cleaning_up", "completed", "failed"}:
                raise BrokerError("task_state_invalid", "Task cannot be terminated in its current state.", 409)
            if task["state"] != "cleaning_up":
                mark_side_effect()
                self.registry.transition(claim.task_id, "cleaning_up")
            try:
                container = self.registry.container_record(claim.task_id)
                if container is None:
                    mark_side_effect()
                    task_root = self.config.state_dir / "tasks" / claim.task_id
                    _remove_task_tree(task_root)
                    self.registry.transition(claim.task_id, "removed")
                    return {"state": "removed", "cancelled_before_start": True}
                if container["lifecycle_state"] not in {"owned", "terminated"}:
                    raise BrokerError("container_quarantined", "Task container allocation is uncertain and cannot be cleaned automatically.", 409)
                mark_side_effect()
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

    app.add_middleware(RequestSizeLimitMiddleware, maximum=MAX_REQUEST_BYTES)
    app.add_middleware(MtlsTransportRequiredMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/v1/ready")
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


class MtlsTransportRequiredMiddleware:
    """Reject cleartext; the loopback listener additionally requires a client certificate."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("scheme") != "https":
            await JSONResponse(
                status_code=426,
                content={"error": {"code": "mtls_required", "message": "Broker API requires mutually authenticated TLS."}},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


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
