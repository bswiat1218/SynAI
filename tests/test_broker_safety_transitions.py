from __future__ import annotations

import asyncio
import errno
import logging
import os
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

from synai.broker.cli import _PinnedLogHandler
from synai.broker.filesystem import Directory, mount_identity
from synai.broker.protocol import BrokerError
from synai.broker.registry import RegistryTrust
from synai.broker.runtime import ContainmentResult
from synai.broker.storage_inspector import InspectorUnavailable
from tests import test_broker as fixtures


class SafetyTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.BrokerServiceTests.asyncSetUp(self)
        self.record = fixtures.BrokerServiceTests._register_owned_container(self)
        self.service.runtime.contain_recorded = AsyncMock(
            return_value=ContainmentResult("verified_stopped", evidence="1" * 64),
        )

    async def asyncTearDown(self):
        await fixtures.BrokerServiceTests.asyncTearDown(self)

    @contextmanager
    def failing_logging(self):
        role = next(role for role in self.selected.storage_roles if role.kind == "broker_logs")
        authority = Directory(
            role.path, owner_uid=role.owner_uid, owner_gid=role.owner_gid,
            private=True, file_verifier=Mock(),
        )
        descriptor = authority.open("broker.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        handler = _PinnedLogHandler(
            os.fdopen(descriptor, "a"), authority, mount_identity(descriptor),
            lambda: self.service._invalidate_storage_readiness("persistent_logging_failed"),
        )
        logger = logging.getLogger("synai.broker.app")
        previous = logger.handlers[:], logger.propagate, logger.level
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        try:
            with patch("synai.broker.cli.os.write", side_effect=OSError(errno.EDQUOT, "quota exhausted")):
                yield handler
        finally:
            logger.handlers[:], logger.propagate, logger.level = previous
            handler.close()

    async def loss(self):
        await self.service._handle_storage_assurance_loss(("inspector_unavailable",))
        self.assertFalse(self.service.readiness.execution_available)

    async def watchdog_outcome(self, *, result=None, error=None):
        entered = asyncio.Event()
        original = self.service._handle_storage_assurance_loss

        async def handle(reasons):
            self.assertFalse(self.service._storage_assurance_lock.locked())
            self.assertFalse(self.service.readiness.execution_available)
            await original(reasons)
            entered.set()

        self.service._watchdog_storage_check = AsyncMock(
            side_effect=error, return_value=result,
        )
        self.service._handle_storage_assurance_loss = handle
        task = asyncio.create_task(self.service._storage_watchdog_loop())
        try:
            await asyncio.wait_for(entered.wait(), 3)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.service._handle_storage_assurance_loss = original
        self.service.runtime.contain_recorded.assert_awaited_once_with(self.record["task_id"])

    async def test_production_handler_failure_preserves_durable_containment(self):
        with self.failing_logging() as handler:
            self.service._registry_failure("injected_failure")
            await self.loss()
            self.assertTrue(handler.failed)
            self.assertFalse(self.service.last_diagnostic_failure["durable"])
            with self.assertRaises(RuntimeError):
                handler.emit(logging.LogRecord("direct", logging.ERROR, __file__, 0, "Still refused", (), None))
            self.assertEqual((handler.authority.path / "broker.log").read_bytes(), b"")
        self.assertEqual(self.service.registry.container_record(self.record["task_id"])["lifecycle_state"], "terminated")

    async def test_logging_and_quarantine_write_failure_still_attempt_containment(self):
        with self.failing_logging() as handler, patch.object(
            self.service.registry, "quarantine_all_for_storage_loss",
            side_effect=sqlite3.OperationalError("write unavailable"),
        ):
            await self.loss()
            self.assertTrue(handler.failed)
        self.service.runtime.contain_recorded.assert_awaited_once()
        self.assertEqual(self.service.registry.container_record(self.record["task_id"])["lifecycle_state"], "owned")
        self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)
        self.assertIn("operator_intervention_required", self.service.readiness.reasons)

    async def test_inspector_registry_and_logging_failure_never_invents_ownership(self):
        with self.failing_logging(), patch.object(
            self.service.registry, "container_records", side_effect=sqlite3.OperationalError("read failed"),
        ), patch.object(
            self.service.registry, "quarantine_all_for_storage_loss", side_effect=sqlite3.OperationalError("write failed"),
        ):
            self.service._watchdog_storage_check = AsyncMock(side_effect=OSError("inspector gone"))
            task = asyncio.create_task(self.service._storage_watchdog_loop())
            try:
                async with asyncio.timeout(3):
                    while self.service.last_diagnostic_failure is None:
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.service.runtime.contain_recorded.assert_not_awaited()
        self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)

    async def test_watchdog_oserror_triggers_containment(self):
        await self.watchdog_outcome(error=OSError("filesystem unavailable"))

    async def test_watchdog_timeout_triggers_containment(self):
        await self.watchdog_outcome(error=TimeoutError("inspection timeout"))

    async def test_watchdog_inspector_unavailable_triggers_containment(self):
        await self.watchdog_outcome(error=InspectorUnavailable("inspector gone"))

    async def test_watchdog_negative_result_triggers_containment(self):
        await self.watchdog_outcome(result=(False, ("quota_drift",)))

    async def test_watchdog_malformed_response_triggers_containment(self):
        await self.watchdog_outcome(result=(True, ["invalid reasons"]))

    async def test_watchdog_unexpected_verification_exception_triggers_containment(self):
        await self.watchdog_outcome(error=TypeError("malformed filesystem metadata"))

    async def test_watchdog_registry_exception_and_logger_failure_trigger_containment(self):
        with self.failing_logging():
            await self.watchdog_outcome(error=sqlite3.OperationalError("registry check failed"))
        self.assertIn("diagnostic_emission_failed", self.service.readiness.reasons)

    async def test_actual_watchdog_registry_preflight_exception_enters_loss(self):
        original = self.service._watchdog_storage_check
        with patch.object(self.service.registry, "verify_ownership", side_effect=OSError("registry unavailable")):
            with self.assertRaises(OSError) as failure:
                await original()
        await self.watchdog_outcome(error=failure.exception)

    async def test_failed_logging_does_not_interrupt_watchdog_restart(self):
        task = asyncio.create_task(asyncio.sleep(0))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.service._storage_watchdog_task = task
        with self.failing_logging():
            self.service._storage_watchdog_finished(task)
        self.assertIsNot(self.service._storage_watchdog_task, task)
        self.assertFalse(self.service.readiness.execution_available)

    async def test_failed_logging_does_not_interrupt_watchdog_exception_callback(self):
        async def fail():
            raise OSError("watchdog failed")

        task = asyncio.create_task(fail())
        with self.assertRaises(OSError):
            await task
        self.service._storage_watchdog_task = task
        with self.failing_logging():
            self.service._storage_watchdog_finished(task)
        self.assertIsNot(self.service._storage_watchdog_task, task)
        self.assertFalse(self.service.readiness.execution_available)

    async def test_repeated_watchdog_failures_share_inflight_containment(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def contain(task_id):
            entered.set()
            await release.wait()
            return ContainmentResult("runtime_unavailable", error_code="unaccepted")

        self.service.runtime.contain_recorded = AsyncMock(side_effect=contain)
        self.service._watchdog_storage_check = AsyncMock(side_effect=OSError("inspection failed"))
        task = asyncio.create_task(self.service._storage_watchdog_loop())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.gather(*(self.loss() for _ in range(3)), self.release_after(release))
            self.assertEqual(self.service.runtime.contain_recorded.await_count, 1)
            self.assertEqual(len(self.service._containment_tasks), 1)
        finally:
            release.set()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    @staticmethod
    async def release_after(release):
        await asyncio.sleep(0.05)
        release.set()

    async def test_shutdown_verification_logging_failure_still_closes_registry(self):
        entered = asyncio.Event()

        async def watchdog():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.service._diagnostic_error("Verification failed during shutdown")

        task = asyncio.create_task(watchdog())
        self.service._storage_watchdog_task = task
        await entered.wait()
        close = self.service.registry.close
        with self.failing_logging(), patch.object(self.service.registry, "close", wraps=close) as closed:
            await self.service.close()
        closed.assert_called_once()
        self.assertTrue(task.done())
        self.assertFalse(self.service.readiness.execution_available)

    async def test_shutdown_pending_containment_logging_failure_preserves_cleanup_policy(self):
        release = asyncio.Event()
        pending = asyncio.create_task(release.wait())
        self.service._containment_tasks[self.record["task_id"]] = pending
        self.service.core_client = Mock(close=AsyncMock())
        try:
            with self.failing_logging() as handler, patch(
                "synai.broker.app.asyncio.wait", new_callable=AsyncMock,
                return_value=(set(), {pending}),
            ), patch.object(self.service.registry, "close") as close:
                await self.service.close()
            self.assertTrue(handler.failed)
            self.service.core_client.close.assert_awaited_once()
            close.assert_not_called()
            self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)
            self.assertFalse(self.service.readiness.execution_available)
        finally:
            release.set()
            await pending

    async def test_containment_evidence_write_and_logging_failure_are_unresolved(self):
        with self.failing_logging(), patch.object(
            self.service.registry, "record_reconciliation",
            side_effect=sqlite3.OperationalError("evidence unavailable"),
        ):
            await self.loss()
        self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)
        self.assertEqual(
            self.service.registry.task_slot(self.record["task_id"], allow_quarantined=True)["state"],
            "quarantined",
        )

    async def test_untrusted_ownership_is_not_a_termination_authority(self):
        self.service.runtime.contain_recorded = fixtures.RootlessPodmanRuntime.contain_recorded.__get__(self.service.runtime)
        self.service.registry.fail(RegistryTrust.MISMATCH, "registry_identity_mismatch")
        with self.failing_logging(), patch.object(self.service.runtime, "_run", new_callable=AsyncMock) as run:
            await self.loss()
        run.assert_not_awaited()
        self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)

    async def test_concurrent_loss_is_single_flight_and_later_success_does_not_restore(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def contain(task_id):
            entered.set()
            await release.wait()
            return ContainmentResult("runtime_unavailable", error_code="unaccepted")

        self.service.runtime.contain_recorded = AsyncMock(side_effect=contain)
        with self.failing_logging():
            first = asyncio.create_task(self.loss())
            await entered.wait()
            second = asyncio.create_task(self.loss())
            await asyncio.sleep(0)
            self.assertEqual(self.service.runtime.contain_recorded.await_count, 1)
            release.set()
            await asyncio.gather(first, second)
        self.service._watchdog_storage_check = AsyncMock(return_value=(True, ()))
        task = asyncio.create_task(self.service._storage_watchdog_loop())
        try:
            await asyncio.sleep(self.selected.storage_watchdog_interval_seconds + 0.05)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.service.readiness.execution_available)
        self.assertEqual(self.service.registry.task_slot(self.record["task_id"], allow_quarantined=True)["state"], "quarantined")

    async def test_watchdog_and_active_monitor_share_one_containment_attempt(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def contain(task_id):
            self.assertFalse(self.service._storage_assurance_lock.locked())
            entered.set()
            await release.wait()
            return ContainmentResult("runtime_unavailable", error_code="unaccepted")

        self.service.runtime.contain_recorded = AsyncMock(side_effect=contain)
        async def execute(*arguments):
            await asyncio.Event().wait()

        self.service._execute = AsyncMock(side_effect=execute)
        self.service.quota_manager.verify_startup = Mock(return_value=(False, ("quota_drift",)))
        self.service._watchdog_storage_check = AsyncMock(return_value=(False, ("quota_drift",)))
        active = asyncio.create_task(self.service._execute_with_lease_monitor(
            fixtures.claim("status", {}), "", {}, None, Mock(),
        ))
        watchdog = asyncio.create_task(self.service._storage_watchdog_loop())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(2.1)
            self.assertEqual(self.service.runtime.contain_recorded.await_count, 1)
            release.set()
            with self.assertRaises(BrokerError):
                await asyncio.wait_for(active, 3)
        finally:
            release.set()
            watchdog.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await watchdog
            if not active.done():
                active.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await active
        self.assertFalse(self.service.readiness.execution_available)

    async def test_admission_loss_handler_releases_global_lock_before_containment(self):
        self.service.quota_manager.verify_startup = Mock(return_value=(False, ("quota_drift",)))
        original = self.service._handle_storage_assurance_loss

        async def handle(reasons):
            self.assertFalse(self.service._storage_assurance_lock.locked())
            await original(reasons)

        self.service._handle_storage_assurance_loss = handle
        request = fixtures.claim("status", {})
        with self.assertRaises(BrokerError) as denied:
            await self.service.dispatch(fixtures.OperationRequest(
                schema_version=1, claim=request.to_dict(),
                claim_signature=fixtures.sign_claim(request, fixtures.CLAIM_PRIVATE),
                kind="status", arguments={},
            ))
        self.assertEqual(denied.exception.code, "storage_inspection_unavailable")
        self.service.runtime.contain_recorded.assert_awaited_once()

    async def test_no_production_subprocess_gate_is_relaxed(self):
        with self.failing_logging(), patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            self.service._diagnostic_error("Safety failure")
            with self.assertRaises(BrokerError) as denied:
                await self.service.runtime._run("inspect", self.record["container_id"], timeout=1, maximum=4096)
        self.assertEqual(denied.exception.code, "runtime_storage_acceptance_pending")
        spawn.assert_not_awaited()

    async def test_diagnostic_boundary_does_not_swallow_cancellation_or_termination(self):
        for error in (asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()):
            with self.subTest(error=type(error).__name__), patch(
                "synai.broker.app.logger.error", side_effect=error,
            ):
                with self.assertRaises(type(error)):
                    self.service._diagnostic_error("Safety failure")
