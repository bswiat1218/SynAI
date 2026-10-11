from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import stat
import time
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from synai.broker.config import BrokerConfig, BrokerReadiness
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

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class TaskWorkspacePaths:
    slot_root: Path
    task_root: Path
    snapshot: Path
    manifest: Path
    workspace: Path


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
        from synai.broker.quota_slots import QuotaSlotManager
        self.quota_manager = QuotaSlotManager(config, self.registry)
        self.runtime.storage_containment_check = (
            lambda: self.quota_manager.runtime_containment_available
        )
        self.runtime.workspace_resolver = lambda task_id: self.task_paths(
            task_id, allow_quarantined=True,
        ).workspace
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
        self._started = False
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._storage_assurance_lock = asyncio.Lock()
        self._storage_watchdog_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._started:
            return
        state_role = next(
            (role for role in self.config.storage_roles if role.kind == "broker_state"),
            None,
        )
        if (
            state_role is None
            or not await asyncio.to_thread(
                self.quota_manager.verify_storage_role, state_role.role_id,
            )
        ):
            self.readiness = _readiness_with_storage(self.config, False)
            self._started = True
            return
        self.registry.initialize()
        quota_ready, _ = self.quota_manager.verify_startup()
        base = _readiness_with_storage(self.config, quota_ready)
        if quota_ready:
            self.readiness = await self.runtime.verify_startup(base)
        else:
            self.readiness = base
        await self.recover()
        if quota_ready:
            self.quota_manager.recover_pending_releases()
            quota_ready, _ = self.quota_manager.verify_startup()
            if quota_ready:
                self.readiness = await self.runtime.verify_startup(
                    _readiness_with_storage(self.config, quota_ready),
                )
            else:
                self.readiness = _readiness_with_storage(self.config, False)
        self._started = True
        self._start_storage_watchdog()

    def task_paths(self, task_id: str, *, allow_quarantined: bool = False) -> TaskWorkspacePaths:
        root = self.quota_manager.resolve_task_root(
            task_id, allow_quarantined=allow_quarantined,
        )
        return TaskWorkspacePaths(
            slot_root=root.parent,
            task_root=root,
            snapshot=root / "snapshot",
            manifest=root / "manifest.json",
            workspace=root / "workspace",
        )

    def release_task_slot(self, task_id: str) -> None:
        task_root = self.quota_manager.verify_releasable(task_id)
        if task_root.exists():
            if any(task_root.iterdir()):
                self.registry.quarantine_slot(task_id)
                raise BrokerError(
                    "workspace_cleanup_incomplete",
                    "Task slot is not empty and cannot be returned to the free pool.",
                    409,
                )
            task_root.rmdir()
        assignment = self.registry.task_slot(task_id)
        evidence = hashlib.sha256(
            f"{task_id}\n{assignment['release_evidence']}\nverified_empty".encode(),
        ).hexdigest()
        selected = next(
            slot for slot in self.config.quota_slots
            if slot.slot_id == assignment["slot_id"]
        )
        self.quota_manager.inspector.inspect(f"task-{selected.slot_id}")
        self.registry.release_slot(task_id, evidence)

    async def close(self) -> None:
        watchdog = self._storage_watchdog_task
        self._storage_watchdog_task = None
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
        if self.core_client is not None:
            await self.core_client.close()

    def _start_storage_watchdog(self) -> None:
        if self._storage_watchdog_task is None:
            task = asyncio.create_task(
                self._storage_watchdog_loop(),
                name="synai-broker-storage-watchdog",
            )
            task.add_done_callback(self._storage_watchdog_finished)
            self._storage_watchdog_task = task

    async def _storage_watchdog_loop(self) -> None:
        interval = self.config.storage_watchdog_interval_seconds
        while True:
            await asyncio.sleep(interval)
            async with self._storage_assurance_lock:
                try:
                    storage_ready, reasons = await asyncio.to_thread(
                        self.quota_manager.verify_startup,
                    )
                except (BrokerError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    self._invalidate_storage_readiness("storage_watchdog_failed")
                    logger.error(
                        "Broker storage watchdog could not verify assurance: %s",
                        type(exc).__name__,
                    )
                    continue
                if not storage_ready:
                    try:
                        await self._handle_storage_assurance_loss(reasons)
                    except (BrokerError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                        self._invalidate_storage_readiness(
                            "storage_loss_handling_failed",
                        )
                        logger.error(
                            "Broker storage assurance loss handling failed: %s",
                            type(exc).__name__,
                        )

    def _storage_watchdog_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            if self._storage_watchdog_task is task:
                self._invalidate_storage_readiness("storage_watchdog_stopped")
                logger.error("Broker storage watchdog was cancelled unexpectedly")
                self._storage_watchdog_task = None
                self._start_storage_watchdog()
            return
        error = task.exception()
        if error is not None:
            self._invalidate_storage_readiness("storage_watchdog_stopped")
            logger.error(
                "Broker storage watchdog stopped unexpectedly: %s",
                type(error).__name__,
            )
            if self._storage_watchdog_task is task:
                self._storage_watchdog_task = None
                self._start_storage_watchdog()

    def _invalidate_storage_readiness(self, reason: str) -> None:
        checks = dict(self.readiness.checks) if self.readiness is not None else {}
        checks["workspace_quota"] = False
        checks["storage_inventory"] = False
        checks["storage_inspector"] = False
        self.readiness = BrokerReadiness(
            False, False, checks,
            tuple(dict.fromkeys((
                *(self.readiness.reasons if self.readiness is not None else ()),
                reason,
            ))),
        )

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
            if record["lifecycle_state"] == "removed":
                try:
                    await self.runtime.verify_removed(task_id)
                except (BrokerError, OSError, TimeoutError) as exc:
                    self.registry.record_reconciliation(
                        task_id, "quarantined",
                        canonical_json({
                            "state": "removal_unverified",
                            "error_code": exc.code if isinstance(exc, BrokerError) else "runtime_unavailable",
                            "observed_at": int(time.time()),
                        }).decode(),
                    )
                    self.registry.quarantine_slot(task_id)
                continue
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
        if not self._started:
            raise BrokerError(
                "broker_not_ready",
                "Broker startup verification has not completed.",
                503,
            )
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
        async with self._storage_assurance_lock:
            if self.readiness is None or not self.readiness.execution_available:
                raise BrokerError(
                    "execution_unavailable",
                    "Broker execution security readiness is not satisfied.",
                    503,
                )
            storage_ready, storage_reasons = await asyncio.to_thread(
                self.quota_manager.verify_startup,
            )
            if not storage_ready:
                await self._handle_storage_assurance_loss(storage_reasons)
                raise BrokerError(
                    "storage_inspection_unavailable",
                    "Fresh bounded-storage inspection is unavailable or inconsistent.",
                    503,
                )
            if claim.tool_kind == "prepare" and (
                self.readiness is None
                or self.readiness.checks.get("workspace_quota") is not True
            ):
                raise BrokerError(
                    "workspace_quota_unverified",
                    "No verified Broker quota slot is available for task preparation.",
                    503,
                )
            eligible_slots = (
                self.quota_manager.verified_slot_ids
                if claim.tool_kind == "prepare"
                else None
            )
            self.registry.authorize(
                claim, payload.claim_signature, eligible_slot_ids=eligible_slots,
            )
        side_effects_started = False
        operation_completed = False
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
            audit_record: dict[str, Any] = {
                "schema_version": 1,
                "operation_id": claim.operation_id,
                "task_id": claim.task_id,
                "status": "completed",
                "result_sha256": hashlib.sha256(encoded).hexdigest(),
                "result_bytes": len(encoded),
            }
            if claim.tool_kind == "collect":
                audit_record["artifact_manifest_path"] = (
                    f"artifacts/{claim.operation_id}.json"
                )
            self.registry.complete_operation(
                claim.operation_id, "completed",
                canonical_json(audit_record).decode("utf-8"),
            )
            operation_completed = True
            if self.registry.task(claim.task_id)["state"] == "removed":
                self.release_task_slot(claim.task_id)
            return {
                "schema_version": 1, "operation_id": claim.operation_id,
                "task_id": claim.task_id, "status": "completed", "result": result,
            }

        except BrokerError:
            if operation_completed:
                raise
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
            if operation_completed:
                raise
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
            if operation_completed:
                raise
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

    async def _handle_storage_assurance_loss(self, reasons: tuple[str, ...]) -> None:
        reason = ",".join(reasons)[:128] or "inspection_unavailable"
        checks = dict(self.readiness.checks) if self.readiness is not None else {}
        checks["workspace_quota"] = False
        checks["storage_inspector"] = False
        checks["storage_inventory"] = False
        self.readiness = BrokerReadiness(
            False, False, checks,
            tuple(dict.fromkeys((*reasons, "storage_containment_pending"))),
        )
        task_ids = self.registry.quarantine_all_for_storage_loss(reason)
        unresolved: list[str] = []
        for task_id in task_ids:
            container = self.registry.container_record(task_id)
            if container is None or container["lifecycle_state"] == "removed":
                continue
            if not self.quota_manager.runtime_containment_available:
                unresolved.append(task_id)
                self.registry.record_reconciliation(
                    task_id, "quarantined",
                    f"storage_loss_runtime_path_unverified:{int(time.time())}",
                )
                continue
            if not container["container_id"]:
                unresolved.append(task_id)
                self.registry.record_reconciliation(
                    task_id, "quarantined", f"storage_loss_no_container_id:{int(time.time())}",
                )
                continue
            try:
                evidence = await self.runtime.terminate_recorded(task_id)
                await self.runtime.inspect_recorded(task_id)
                self.registry.record_reconciliation(
                    task_id, "terminated", f"storage_loss_verified_stop:{evidence}",
                )
            except (BrokerError, OSError, TimeoutError) as exc:
                unresolved.append(task_id)
                code = exc.code if isinstance(exc, BrokerError) else "runtime_unavailable"
                self.registry.record_reconciliation(
                    task_id, "quarantined",
                    f"storage_loss_containment_unconfirmed:{code}:{int(time.time())}",
                )
        if unresolved:
            checks = dict(self.readiness.checks)
            checks["storage_containment_confirmed"] = False
            self.readiness = BrokerReadiness(
                False, False, checks,
                tuple(dict.fromkeys((*self.readiness.reasons, "storage_containment_unconfirmed"))),
            )

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
                async with self._storage_assurance_lock:
                    try:
                        storage_ready, storage_reasons = await asyncio.to_thread(
                            self.quota_manager.verify_startup,
                        )
                    except (BrokerError, OSError, ValueError, RuntimeError, sqlite3.Error):
                        storage_ready = False
                        storage_reasons = ("storage_verification_failed",)
                    if not storage_ready:
                        await self._handle_storage_assurance_loss(storage_reasons)
                if not storage_ready:
                    operation.cancel()
                    try:
                        await operation
                    except (
                        asyncio.CancelledError, BrokerError, OSError, TimeoutError,
                    ):
                        pass
                    raise BrokerError(
                        "storage_inspection_unavailable",
                        "Storage assurance was lost during task execution.",
                        503,
                    )
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
            task_root = self.quota_manager.create_task_root(claim.task_id)
            paths = self.task_paths(claim.task_id)
            if paths.task_root != task_root:
                self.registry.quarantine_slot(claim.task_id)
                raise BrokerError(
                    "workspace_slot_mismatch",
                    "Created task directory differs from its durable quota slot.",
                    409,
                )
            stage_snapshot(paths.task_root, manifest, files)
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
            workspace = self.task_paths(claim.task_id).workspace
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
            paths = self.task_paths(claim.task_id)
            workspace = paths.workspace
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
            paths = self.task_paths(claim.task_id)
            workspace = paths.workspace
            manifest = load_staged_manifest(
                paths.task_root, task["snapshot_digest"],
            )
            changes = changed_files(
                workspace, manifest, self.config.profile.workspace_bytes,
                self.config.profile.output_bytes,
            )
            artifact_bytes = canonical_json(changes)
            artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
            mark_side_effect()
            _persist_artifact_evidence(
                paths.task_root, claim.operation_id, artifact_bytes,
            )
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
                    task_root = self.quota_manager.verify_releasable(claim.task_id)
                    _remove_task_tree(task_root)
                    self.registry.transition(claim.task_id, "removed")
                    release_evidence = hashlib.sha256(
                        f"{claim.allocation_id}\ncancelled_before_start".encode(),
                    ).hexdigest()
                    self.registry.mark_slot_release_pending(
                        claim.task_id, claim.operation_id, release_evidence,
                    )
                    return {"state": "removed", "cancelled_before_start": True}
                if container["lifecycle_state"] not in {"owned", "terminated"}:
                    raise BrokerError("container_quarantined", "Task container allocation is uncertain and cannot be cleaned automatically.", 409)
                mark_side_effect()
                evidence = await self.runtime.terminate_owned(claim.task_id)
                await self.runtime.wait_cancelled_operation(claim.task_id)
                if kind == "remove":
                    cleanup = await self.runtime.remove_owned(claim.task_id)
                    self.registry.transition(claim.task_id, "removed")
                    task_root = self.quota_manager.verify_releasable(claim.task_id)
                    _remove_task_tree(task_root)
                    self.registry.mark_slot_release_pending(
                        claim.task_id, claim.operation_id, cleanup,
                    )
                    return {"state": "removed", "termination_evidence": evidence, "cleanup_evidence": cleanup}
                return {"state": "cleaning_up", "termination_evidence": evidence}
            except BrokerError:
                if self.registry.task(claim.task_id)["state"] == "cleaning_up":
                    self.registry.transition(claim.task_id, "uncertain")
                raise
        raise BrokerError("tool_not_allowed", "Operation kind is not supported.", 403)


def create_app(service: BrokerService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="SynAI Sandbox Broker", version="1", lifespan=lifespan)
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
    import sqlite3
    shutil.rmtree(task_root)


def _readiness_with_storage(config: BrokerConfig, storage_ready: bool) -> BrokerReadiness:
    base = config.readiness()
    checks = dict(base.checks)
    checks["workspace_quota"] = storage_ready
    checks["storage_inventory"] = bool(config.storage_roles) and storage_ready
    checks["storage_inspector"] = config.inspector_socket is not None and storage_ready
    reasons = tuple(name for name, passed in checks.items() if not passed)
    return BrokerReadiness(not reasons, not reasons, checks, reasons)


def _persist_artifact_evidence(task_root: Path, operation_id: str, content: bytes) -> None:
    if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise BrokerError("artifact_identity_invalid", "Artifact operation identity is invalid.", 500)
    artifacts = task_root / "artifacts"
    try:
        artifacts.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        info = artifacts.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise BrokerError(
                "artifact_storage_unsafe",
                "Task artifact evidence directory is not safely owned.",
                409,
            )
        directory_fd = os.open(
            artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            info = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise BrokerError(
                    "artifact_storage_unsafe",
                    "Task artifact evidence directory changed during validation.",
                    409,
                )
            descriptor = os.open(
                f"{operation_id}.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                view = memoryview(content)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("Artifact evidence write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BrokerError:
        raise
    except OSError as exc:
        raise BrokerError(
            "artifact_storage_unavailable",
            "Task artifact evidence could not be durably stored inside its quota slot.",
            507,
        ) from exc
