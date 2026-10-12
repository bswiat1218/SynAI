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
from synai.broker.registry import BrokerRegistry, RegistryTrust
from synai.broker.filesystem import Directory, directory_access, remove_tree, write_new
from synai.broker.runtime import ContainmentResult, RootlessPodmanRuntime
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
        self.quota_manager.persistent_file_failure_callback = self._invalidate_storage_readiness
        self.runtime.storage_containment_check = (
            lambda: self.quota_manager.runtime_containment_available
        )
        self.runtime.workspace_resolver = lambda task_id: self.task_paths(
            task_id, allow_quarantined=True,
        ).workspace
        self.runtime.workspace_authority = self._workspace_authority
        self.runtime.storage_authority = lambda role_id: self.quota_manager.open_storage_role(role_id)
        self.runtime.storage_path_verifier = lambda role_id, path: self.quota_manager.verify_runtime_path(role_id, path)
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
        self._containment_tasks: dict[str, asyncio.Task[ContainmentResult]] = {}
        self._startup_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._lifecycle_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._lifespan_active = False
        self.last_diagnostic_failure: dict[str, str | int | bool] | None = None
        self.registry.failure_callback = self._registry_failure
        self.registry.storage_check = self._registry_storage_verified
        self.registry.directory_factory = self._registry_directory

    def _registry_directory(self) -> Directory:
        role = next(role for role in self.config.storage_roles if role.kind == "broker_state")
        return self.quota_manager.open_storage_role(role.role_id)

    def _registry_storage_verified(self) -> bool:
        role = next(
            (role for role in self.config.storage_roles if role.kind == "broker_state"),
            None,
        )
        return role is not None and self.quota_manager.verify_storage_role(role.role_id)

    def _workspace_authority(self, task_id: str) -> Directory:
        with self.quota_manager.open_task_root(task_id, allow_quarantined=True) as root:
            return root.child_directory("workspace")

    def _registry_failure(self, code: str) -> None:
        self._invalidate_storage_readiness(code)
        self._invalidate_storage_readiness("operator_intervention_required")
        self._invalidate_storage_readiness("storage_containment_unconfirmed")
        self._diagnostic_error("Broker registry authority revoked: %s; containment unresolved", code)

    def _diagnostic_error(self, message: str, *arguments: object) -> None:
        try:
            logger.error(message, *arguments)
        except Exception as exc:
            # Only diagnostic emission is best effort; security operations stay outside this boundary.
            self.last_diagnostic_failure = {
                "message": message[:256],
                "error_type": type(exc).__name__,
                "observed_at": int(time.time()),
                "durable": False,
            }
            self._invalidate_storage_readiness("diagnostic_emission_failed")

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._closed or self._lifecycle_loop is not None and self._lifecycle_loop is not loop:
            self._invalidate_storage_readiness("service_lifecycle_invalid")
            raise BrokerError("service_lifecycle_invalid", "Broker service requires a fresh verified lifecycle.", 503)
        self._lifecycle_loop = loop
        async with self._startup_lock:
            await self._start()

    async def _start(self) -> None:
        if self._started:
            return
        self.readiness = _readiness_with_storage(self.config, False)
        try:
            if not await asyncio.to_thread(self._registry_storage_verified):
                self.registry.fail(RegistryTrust.STORAGE_UNVERIFIED, "state_storage_unverified")
                return
            self.registry.initialize(storage_verified=True)
            self.registry.trust = RegistryTrust.RECOVERY_INCOMPLETE
            quota_ready, _ = self.quota_manager.verify_startup()
            base = _readiness_with_storage(self.config, quota_ready)
            if quota_ready:
                self.readiness = await self.runtime.verify_startup(base)
            await self.recover()
            if quota_ready and self.registry.trust == RegistryTrust.COMPLETE:
                self.quota_manager.recover_pending_releases()
                quota_ready, _ = self.quota_manager.verify_startup()
                if quota_ready:
                    self.readiness = await self.runtime.verify_startup(
                        _readiness_with_storage(self.config, quota_ready),
                    )
                else:
                    self.readiness = _readiness_with_storage(self.config, False)
            if self.registry.failure_code is not None or self.registry.trust != RegistryTrust.COMPLETE:
                self._registry_failure(self.registry.failure_code or "recovery_incomplete")
            self._invalidate_storage_readiness("external_filesystem_resolution_unaccepted")
            self._invalidate_storage_readiness("runtime_storage_acceptance_pending")
        except (BrokerError, OSError, sqlite3.Error, ValueError) as exc:
            if self.registry.failure_code is None:
                self.registry._access_failed(exc)
        finally:
            self._invalidate_storage_readiness("sqlite_file_quota_vfs_unaccepted")
            self._started = True
            self._start_storage_watchdog()

    def task_paths(self, task_id: str, *, allow_quarantined: bool = False) -> TaskWorkspacePaths:
        root = self.quota_manager.task_path_for_inspection(
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
        self.quota_manager.remove_empty_task_root(task_id)
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
        self._closed = True
        self._invalidate_storage_readiness("service_stopped")
        watchdog = self._storage_watchdog_task
        self._storage_watchdog_task = None
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
        containment_tasks = tuple(self._containment_tasks.values())
        pending = set()
        if containment_tasks:
            done, pending = await asyncio.wait(containment_tasks, timeout=45)
            if pending:
                self._invalidate_storage_readiness(
                    "storage_containment_unconfirmed",
                )
                self._diagnostic_error(
                    "Broker shutdown left %d exact-container containment attempt(s) unresolved",
                    len(pending),
                )
            for task in done:
                if not task.cancelled():
                    task.exception()
        if self.core_client is not None:
            await self.core_client.close()
        if not pending:
            self.registry.close()

    def _start_storage_watchdog(self) -> None:
        if not self._closed and self._storage_watchdog_task is None:
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
                    result = await self._watchdog_storage_check()
                    if (
                        not isinstance(result, tuple) or len(result) != 2
                        or type(result[0]) is not bool
                        or not isinstance(result[1], tuple)
                        or not all(isinstance(reason, str) and reason for reason in result[1])
                    ):
                        raise ValueError("Storage verification returned malformed evidence")
                    storage_ready, reasons = result
                    if self.runtime.storage_policy.observed is not None or self.runtime.storage_policy.failure_code:
                        runtime_ready, runtime_reasons = await self.runtime.verify_storage_assurance()
                        storage_ready = storage_ready and runtime_ready
                        reasons = (*reasons, *runtime_reasons)
                except Exception as exc:
                    self._invalidate_storage_readiness("storage_watchdog_failed")
                    storage_ready = False
                    reasons = ("storage_watchdog_failed",)
                    verification_error = type(exc).__name__
                else:
                    verification_error = None
                if not storage_ready:
                    self._invalidate_storage_readiness("storage_assurance_lost")
            if not storage_ready:
                try:
                    await self._handle_storage_assurance_loss(reasons)
                except (BrokerError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    self._invalidate_storage_readiness(
                        "storage_loss_handling_failed",
                    )
                    self._diagnostic_error(
                        "Broker storage assurance loss handling failed: %s",
                        type(exc).__name__,
                    )
                if verification_error is not None:
                    self._diagnostic_error(
                        "Broker storage watchdog could not verify assurance: %s",
                        verification_error,
                    )

    async def _watchdog_storage_check(self) -> tuple[bool, tuple[str, ...]]:
        def verify() -> tuple[bool, tuple[str, ...]]:
            if self.registry.failure_code is None:
                self.registry.verify_ownership()
            return self.quota_manager.verify_startup()

        verification = asyncio.create_task(asyncio.to_thread(verify))
        try:
            return await asyncio.shield(verification)
        except asyncio.CancelledError:
            # Cancelling the asyncio waiter cannot stop its filesystem/inspector thread.
            # Join the in-flight check before closing registry descriptors.
            try:
                await verification
            except (BrokerError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                self._invalidate_storage_readiness("storage_watchdog_failed")
                self._diagnostic_error("Broker storage verification failed during shutdown: %s", type(exc).__name__)
            raise

    def _storage_watchdog_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            if self._storage_watchdog_task is task:
                self._invalidate_storage_readiness("storage_watchdog_stopped")
                self._storage_watchdog_task = None
                self._start_storage_watchdog()
                self._diagnostic_error("Broker storage watchdog was cancelled unexpectedly")
            return
        error = task.exception()
        if error is not None:
            self._invalidate_storage_readiness("storage_watchdog_stopped")
            if self._storage_watchdog_task is task:
                self._storage_watchdog_task = None
                self._start_storage_watchdog()
            self._diagnostic_error(
                "Broker storage watchdog stopped unexpectedly: %s",
                type(error).__name__,
            )

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
        async with self._recovery_lock:
            self._invalidate_storage_readiness("recovery_incomplete")
            if self.registry.failure_code is None:
                self.registry.trust = RegistryTrust.RECOVERY_INCOMPLETE
            try:
                self.registry.verify_ownership()
                await self._recover()
                if (
                    self.registry.has_quarantined_slots()
                    or self.registry.unassigned_legacy_tasks()
                    or any(row["lifecycle_state"] not in {"terminated", "removed"}
                           for row in self.registry.container_records())
                ):
                    self.registry.fail(RegistryTrust.RECOVERY_INCOMPLETE, "recovery_incomplete")
                elif self.registry.failure_code is None:
                    self.registry.trust = RegistryTrust.COMPLETE
            except asyncio.CancelledError:
                self.registry.fail(RegistryTrust.RECOVERY_INCOMPLETE, "recovery_interrupted")
                raise
            except (BrokerError, OSError, sqlite3.Error, ValueError) as exc:
                self.registry._access_failed(exc)
                raise

    async def _recover(self) -> None:
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
        if self._closed:
            raise BrokerError("broker_not_ready", "Broker service has stopped.", 503)
        if not self._started:
            raise BrokerError(
                "broker_not_ready",
                "Broker startup verification has not completed.",
                503,
            )
        if self.registry.failure_code is not None:
            raise BrokerError("registry_unavailable", "Registry authority is revoked; operator recovery is required.", 503)
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
            if self.runtime.storage_policy.observed is not None or self.runtime.storage_policy.failure_code:
                runtime_ready, runtime_reasons = await self.runtime.verify_storage_assurance()
                storage_ready = storage_ready and runtime_ready
                storage_reasons = (*storage_reasons, *runtime_reasons)
            if not storage_ready:
                self._invalidate_storage_readiness("storage_assurance_lost")
            elif claim.tool_kind == "prepare" and (
                self.readiness is None
                or self.readiness.checks.get("workspace_quota") is not True
            ):
                raise BrokerError(
                    "workspace_quota_unverified",
                    "No verified Broker quota slot is available for task preparation.",
                    503,
                )
            if storage_ready:
                eligible_slots = (
                    self.quota_manager.verified_slot_ids
                    if claim.tool_kind == "prepare"
                    else None
                )
                self.registry.verify_ownership()
                self.registry.authorize(
                    claim, payload.claim_signature, eligible_slot_ids=eligible_slots,
                )
        if not storage_ready:
            await self._handle_storage_assurance_loss(storage_reasons)
            raise BrokerError(
                "storage_inspection_unavailable",
                "Fresh bounded-storage inspection is unavailable or inconsistent.",
                503,
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
        self._invalidate_storage_readiness("storage_assurance_lost")
        self.readiness = BrokerReadiness(
            False, False, dict(self.readiness.checks),
            tuple(dict.fromkeys((*self.readiness.reasons, *reasons))),
        )
        durable_quarantine = False
        container_records: tuple[Any, ...] = ()
        task_ids: tuple[str, ...] = ()
        container_records_available = False
        quarantine_error = None
        try:
            container_records = self.registry.container_records()
            container_records_available = True
        except (BrokerError, OSError, sqlite3.Error, ValueError):
            pass
        try:
            task_ids = self.registry.quarantine_all_for_storage_loss(reason)
            durable_quarantine = True
        except (BrokerError, OSError, sqlite3.Error, ValueError) as exc:
            quarantine_error = type(exc).__name__
            self._invalidate_storage_readiness("operator_intervention_required")
            if container_records:
                task_ids = tuple(dict.fromkeys(
                    str(row["task_id"]) for row in container_records
                ))

        records_by_task = {
            str(record["task_id"]): record for record in container_records
        }
        pending: list[asyncio.Task[ContainmentResult]] = []
        unresolved: list[str] = (
            ["registry_quarantine_not_durable"] if not durable_quarantine else []
        )
        if not container_records_available:
            unresolved.append("registry_ownership_unavailable")
            self._invalidate_storage_readiness("registry_ownership_unavailable")
            self._invalidate_storage_readiness("operator_intervention_required")
        if not durable_quarantine:
            self._invalidate_storage_readiness("registry_quarantine_not_durable")
        for task_id in task_ids:
            container = records_by_task.get(task_id)
            if container is None:
                if not container_records_available:
                    unresolved.append(task_id)
                continue
            if container["lifecycle_state"] == "removed":
                continue
            if not container["container_id"]:
                unresolved.append(task_id)
                self._persist_containment_unresolved(
                    task_id, "identity_mismatch", "recorded_container_id_missing",
                    durable_quarantine=durable_quarantine,
                )
                continue
            pending.append(self._containment_task(
                task_id, durable_quarantine=durable_quarantine,
            ))

        if quarantine_error is not None:
            self._diagnostic_error(
                "Broker could not persist storage-loss quarantine: %s",
                quarantine_error,
            )
        if pending:
            done, still_pending = await asyncio.wait(pending, timeout=45)
            if still_pending:
                unresolved.extend(
                    task_id for task_id, task in self._containment_tasks.items()
                    if task in still_pending
                )
            for task in done:
                try:
                    outcome = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    task_id = next(
                        (key for key, value in self._containment_tasks.items() if value is task),
                        "",
                    )
                    if task_id:
                        unresolved.append(task_id)
                        self._persist_containment_unresolved(
                            task_id, "inspection_inconclusive",
                            type(exc).__name__,
                            durable_quarantine=durable_quarantine,
                        )
                    continue
                task_id = next(
                    (key for key, value in self._containment_tasks.items() if value is task),
                    "",
                )
                if not task_id:
                    continue
                if not outcome.verified:
                    unresolved.append(task_id)
        if unresolved:
            checks = dict(self.readiness.checks)
            checks["storage_containment_confirmed"] = False
            self.readiness = BrokerReadiness(
                False, False, checks,
                tuple(dict.fromkeys((*self.readiness.reasons, "storage_containment_unconfirmed"))),
            )
        else:
            checks = dict(self.readiness.checks)
            checks["storage_containment_confirmed"] = True
            self.readiness = BrokerReadiness(
                False, False, checks,
                tuple(dict.fromkeys((*self.readiness.reasons, "storage_assurance_lost"))),
            )

    def _containment_task(
        self, task_id: str, *, durable_quarantine: bool,
    ) -> asyncio.Task[ContainmentResult]:
        existing = self._containment_tasks.get(task_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._contain_and_persist(
                task_id, durable_quarantine=durable_quarantine,
            ),
            name=f"synai-broker-containment-{task_id}",
        )
        self._containment_tasks[task_id] = task
        return task

    async def _contain_and_persist(
        self, task_id: str, *, durable_quarantine: bool,
    ) -> ContainmentResult:
        try:
            outcome = await self.runtime.contain_recorded(task_id)
        except asyncio.CancelledError:
            self._persist_containment_unresolved(
                task_id, "inspection_inconclusive", "containment_cancelled",
                durable_quarantine=durable_quarantine,
            )
            raise
        except Exception as exc:
            self._persist_containment_unresolved(
                task_id, "inspection_inconclusive", type(exc).__name__,
                durable_quarantine=durable_quarantine,
            )
            return ContainmentResult(
                "inspection_inconclusive", error_code="containment_operation_failed",
            )
        if not self._persist_containment_result(
            task_id, outcome, durable_quarantine=durable_quarantine,
        ):
            return ContainmentResult(
                "inspection_inconclusive", error_code="containment_state_not_durable",
            )
        return outcome

    def _persist_containment_result(
        self,
        task_id: str,
        outcome: ContainmentResult,
        *,
        durable_quarantine: bool,
    ) -> bool:
        if not durable_quarantine:
            self._persist_containment_unresolved(
                task_id, outcome.state, outcome.error_code or "quarantine_not_durable",
                durable_quarantine=False,
            )
            return False
        evidence = canonical_json({
            "state": outcome.state,
            "evidence": outcome.evidence,
            "error_code": outcome.error_code,
            "observed_at": int(time.time()),
        }).decode("utf-8")
        try:
            self.registry.record_reconciliation(
                task_id,
                "terminated" if outcome.verified else "quarantined",
                evidence,
            )
        except (BrokerError, OSError, sqlite3.Error, ValueError) as exc:
            self._diagnostic_error(
                "Broker could not persist containment result for an owned container: %s",
                type(exc).__name__,
            )
            return False
        return True

    def _persist_containment_unresolved(
        self,
        task_id: str,
        state: str,
        error_code: str,
        *,
        durable_quarantine: bool,
    ) -> None:
        if not durable_quarantine:
            return
        try:
            self.registry.record_reconciliation(
                task_id, "quarantined",
                canonical_json({
                    "state": state,
                    "error_code": error_code[:64],
                    "observed_at": int(time.time()),
                }).decode("utf-8"),
            )
        except (BrokerError, OSError, sqlite3.Error, ValueError) as exc:
            self._diagnostic_error(
                "Broker could not persist unresolved containment state: %s",
                type(exc).__name__,
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
                        if self.runtime.storage_policy.observed is not None or self.runtime.storage_policy.failure_code:
                            runtime_ready, runtime_reasons = await self.runtime.verify_storage_assurance()
                            storage_ready = storage_ready and runtime_ready
                            storage_reasons = (*storage_reasons, *runtime_reasons)
                    except (BrokerError, OSError, ValueError, RuntimeError, sqlite3.Error):
                        storage_ready = False
                        storage_reasons = ("storage_verification_failed",)
                    if not storage_ready:
                        self._invalidate_storage_readiness("storage_assurance_lost")
                if not storage_ready:
                    await self._handle_storage_assurance_loss(storage_reasons)
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
        try:
            return await self._execute_scoped(claim, arguments, signature, snapshot_grant, mark_side_effect)
        except BrokerError as exc:
            if exc.code.startswith(("filesystem_", "openat2_", "workspace_path_", "bind_source_")):
                self._invalidate_storage_readiness("filesystem_authority_lost")
                self.registry.quarantine_slot(claim.task_id)
            raise
        except OSError as exc:
            if exc.errno in {18, 20, 40}:
                self._invalidate_storage_readiness("filesystem_authority_lost")
                self.registry.quarantine_slot(claim.task_id)
            raise

    async def _execute_scoped(
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
            with self.quota_manager.create_task_root(claim.task_id) as task_root:
                stage_snapshot(task_root, manifest, files)
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
            if kind in {"write_file", "patch_file", "delete_file"}:
                mark_side_effect()
            with self.quota_manager.open_task_root(claim.task_id) as root, root.child_directory("workspace") as workspace:
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
            with self.quota_manager.open_task_root(claim.task_id) as root:
                manifest = load_staged_manifest(root, task["snapshot_digest"])
                with root.child_directory("workspace") as workspace:
                    changes = changed_files(
                        workspace, manifest, self.config.profile.workspace_bytes,
                        self.config.profile.output_bytes,
                    )
                artifact_bytes = canonical_json(changes)
                artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
                mark_side_effect()
                _persist_artifact_evidence(root, claim.operation_id, artifact_bytes)
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
                    self.quota_manager.clean_task_root(claim.task_id)
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
                    self.quota_manager.clean_task_root(claim.task_id)
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


def create_app(service: BrokerService, *, startup_callback: Callable[[], Callable[[], None] | None] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if service._lifespan_active:
            raise BrokerError("service_lifecycle_invalid", "Broker service already has an active ASGI lifecycle.", 503)
        service._lifespan_active = True
        close_logging = None
        try:
            if startup_callback is not None:
                close_logging = startup_callback()
            await service.start()
            yield
        finally:
            try:
                await service.close()
            finally:
                try:
                    if close_logging is not None:
                        close_logging()
                finally:
                    service._lifespan_active = False

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
            return {
                "schema_version": 1, "ready": False, "execution_available": False,
                "reasons": ["startup_verification_incomplete"],
                "registry": service.registry.status(),
                "diagnostics": {"last_emission_failure": service.last_diagnostic_failure},
            }
        return {
            "schema_version": 1, "ready": state.ready,
            "execution_available": state.execution_available,
            "checks": state.checks, "reasons": list(state.reasons),
            "registry": service.registry.status(),
            "diagnostics": {"last_emission_failure": service.last_diagnostic_failure},
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


def _remove_task_tree(task_root: Path | Directory) -> None:
    with directory_access(task_root) as root:
        remove_tree(root)


def _readiness_with_storage(config: BrokerConfig, storage_ready: bool) -> BrokerReadiness:
    base = config.readiness()
    checks = dict(base.checks)
    checks["workspace_quota"] = storage_ready
    checks["storage_inventory"] = bool(config.storage_roles) and storage_ready
    checks["storage_inspector"] = config.inspector_socket is not None and storage_ready
    reasons = tuple(name for name, passed in checks.items() if not passed)
    return BrokerReadiness(not reasons, not reasons, checks, reasons)


def _persist_artifact_evidence(task_root: Path | Directory, operation_id: str, content: bytes) -> None:
    if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise BrokerError("artifact_identity_invalid", "Artifact operation identity is invalid.", 500)
    try:
        with directory_access(task_root) as root:
            try:
                artifacts = root.mkdir("artifacts")
            except FileExistsError:
                artifacts = root.child_directory("artifacts")
            with artifacts:
                write_new(artifacts, f"{operation_id}.json", content)
    except BrokerError:
        raise
    except OSError as exc:
        raise BrokerError(
            "artifact_storage_unavailable",
            "Task artifact evidence could not be durably stored inside its quota slot.",
            507,
        ) from exc
