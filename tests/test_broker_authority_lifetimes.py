from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from synai.broker.app import BrokerService
from synai.broker.processes import ChildProcesses
from synai.broker.protocol import BrokerError
from synai.broker.quota_slots import QuotaSlotManager
from synai.broker.registry import BrokerRegistry
from synai.broker.workers import AuthorityWorkers
from tests.test_broker import config, claim, sign_claim, CLAIM_PRIVATE, OperationRequest
from tests.test_broker_runtime_storage import quota_observations
from tests.test_broker import FakeQuotaManager
from tests import test_broker_safety_transitions as safety


class AuthorityLatchTests(unittest.TestCase):
    def test_failure_during_successful_verification_cannot_repopulate_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = config(Path(temporary) / "broker", slot_count=4)
            registry = BrokerRegistry(selected)
            registry.initialize(storage_verified=True, provision=True)
            observed = quota_observations(selected)
            entered, release = threading.Event(), threading.Event()

            def inspect(role_id):
                entered.set()
                if not release.wait(3):
                    raise OSError("fixture expired")
                return observed[role_id]

            manager = QuotaSlotManager(selected, registry, Mock(inspect=inspect))
            manager._production_inspector = True
            outcome = []
            with (
                patch("synai.broker.quota_slots._process_identity_is_restricted", return_value=True),
                patch.object(manager, "_verify_local_role_path"),
                patch.object(manager, "verify_existing_files"),
                patch.object(manager, "_verify_slot_contents"),
            ):
                worker = threading.Thread(target=lambda: outcome.append(manager.verify_startup()))
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    manager._persistent_file_failed(selected.storage_roles[0].role_id, "filesystem_file_quota_mismatch")
                    self.assertFalse(manager.verified_slot_ids)
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(outcome[0][0])
                self.assertFalse(manager.verified_slot_ids)
            registry.close()

    def test_corrected_evidence_never_restores_local_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = config(Path(temporary) / "broker", slot_count=4)
            registry = BrokerRegistry(selected)
            registry.initialize(storage_verified=True, provision=True)
            observed = quota_observations(selected)
            manager = QuotaSlotManager(selected, registry, Mock(inspect=lambda role: observed[role]))
            manager._production_inspector = True
            with (
                patch("synai.broker.quota_slots._process_identity_is_restricted", return_value=True),
                patch.object(manager, "_verify_local_role_path"),
                patch.object(manager, "verify_existing_files"),
                patch.object(manager, "_verify_slot_contents"),
            ):
                self.assertTrue(manager.verify_startup()[0])
                role = next(role for role in selected.storage_roles if role.kind == "broker_logs")
                manager._persistent_file_failed(role.role_id, "filesystem_file_quota_mismatch")
                for _ in range(2):
                    manager._persistent_file_failed(role.role_id, "later_failure")
                    self.assertFalse(manager.verify_startup()[0])
                    self.assertFalse(manager.verified_slot_ids)
                    self.assertFalse(manager.verify_storage_role(role.role_id))
                    self.assertIn("filesystem_file_quota_mismatch", manager.reasons)
                self.assertTrue(manager.runtime_containment_available)
                for operation in (
                    lambda: manager.open_storage_role(role.role_id),
                    lambda: manager.open_task_root("a" * 32),
                    lambda: manager.create_task_root("a" * 32),
                    lambda: manager.clean_task_root("a" * 32),
                    lambda: manager.remove_empty_task_root("a" * 32),
                    manager.recover_pending_releases,
                ):
                    with self.assertRaises(BrokerError):
                        operation()
            registry.close()
            restarted = BrokerRegistry(selected)
            restarted.initialize(storage_verified=True)
            self.assertTrue(restarted.path.exists())
            restarted.close()

    def test_concurrent_file_failures_retain_first_reason_and_no_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = config(Path(temporary) / "broker")
            manager = QuotaSlotManager(selected, BrokerRegistry(selected))
            manager._persistent_file_failed("broker-logs", "first_failure")
            threads = [threading.Thread(target=manager._persistent_file_failed, args=("broker-logs", "later")) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(manager.persistent_file_failure_code, "first_failure")
            self.assertFalse(manager.verified)
            self.assertFalse(manager.verified_slot_ids)


class WorkerLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_failure_followed_by_good_watchdog_evidence_still_refuses_allocation(self):
        from synai.broker.config import BrokerReadiness

        with tempfile.TemporaryDirectory() as temporary:
            selected = config(Path(temporary) / "broker", slot_count=4)
            service = BrokerService(selected)
            service.registry.directory_factory = None
            service.registry.storage_check = None
            service.registry.initialize(storage_verified=True, provision=True)
            observed = quota_observations(selected)
            manager = QuotaSlotManager(selected, service.registry, Mock(inspect=lambda role: observed[role]))
            manager._production_inspector = True
            manager.persistent_file_failure_callback = service._invalidate_storage_readiness
            service.quota_manager = manager
            service.readiness = BrokerReadiness(True, True, {"workspace_quota": True}, ())
            service._started = True
            service.lease_validator = AsyncMock(return_value=True)
            handled = asyncio.Event()
            original = service._handle_storage_assurance_loss

            async def handle(reasons):
                await original(reasons)
                handled.set()

            service._handle_storage_assurance_loss = handle
            try:
                with (
                    patch("synai.broker.quota_slots._process_identity_is_restricted", return_value=True),
                    patch.object(manager, "_verify_local_role_path"),
                    patch.object(manager, "verify_existing_files"),
                    patch.object(manager, "_verify_slot_contents"),
                ):
                    self.assertTrue(manager.verify_startup()[0])
                    role = next(role for role in selected.storage_roles if role.kind == "broker_logs")
                    manager._persistent_file_failed(role.role_id, "filesystem_file_quota_mismatch")
                    self.assertFalse(manager.verify_startup()[0])
                    service._watchdog_storage_check = AsyncMock(return_value=manager.verify_startup())
                    service._start_storage_watchdog()
                    await asyncio.wait_for(handled.wait(), 3)
                    request = claim("prepare", {})
                    with self.assertRaises(BrokerError) as denied:
                        await service.dispatch(OperationRequest(
                            schema_version=1, claim=request.to_dict(),
                            claim_signature=sign_claim(request, CLAIM_PRIVATE),
                            kind="prepare", arguments={},
                        ))
                    self.assertEqual(denied.exception.code, "execution_unavailable")
                    self.assertFalse(manager.verified_slot_ids)
                    self.assertTrue(all(row["state"] == "free" for row in service.registry.slot_configurations()))
            finally:
                await service.close()

    async def test_logging_and_worker_exception_during_shutdown_preserve_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.selected = config(Path(temporary) / "broker")
            self.service = BrokerService(self.selected)
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()

            def check():
                loop.call_soon_threadsafe(entered.set)
                release.wait(3)
                raise OSError("registry unavailable")

            task = asyncio.create_task(self.service.authority_workers.run(check))
            await entered.wait()
            try:
                with safety.SafetyTransitionTests.failing_logging(self) as handler:
                    self.service._diagnostic_error("Worker shutdown failure")
                    task.cancel()
                    closing = asyncio.create_task(self.service.close())
                    await asyncio.sleep(0)
                    self.assertTrue(handler.failed)
                    self.assertFalse(closing.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await closing
                self.assertIn("authority_worker_failed", self.service.readiness.reasons)
                self.assertFalse(self.service.readiness.execution_available)
            finally:
                release.set()
                await self.service.close()

    async def service_worker_case(self, kind):
        with tempfile.TemporaryDirectory() as temporary:
            service = BrokerService(config(Path(temporary) / "broker"))
            service.quota_manager = FakeQuotaManager(service.config, service.registry)
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()

            def check():
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise OSError("fixture expired")
                return True, ()

            service.quota_manager.verify_startup = check
            if kind == "watchdog":
                service.registry.verify_ownership = Mock()
                operation = service._watchdog_storage_check()
            elif kind == "admission":
                from synai.broker.config import BrokerReadiness
                service.readiness = BrokerReadiness(True, True, {"workspace_quota": True}, ())
                service._started = True
                service.lease_validator = AsyncMock(return_value=True)
                request = claim("status", {})
                operation = service.dispatch(OperationRequest(
                    schema_version=1, claim=request.to_dict(),
                    claim_signature=sign_claim(request, CLAIM_PRIVATE), kind="status", arguments={},
                ))
            else:
                service.lease_validator = AsyncMock(return_value=True)
                async def execute(*arguments):
                    await asyncio.Event().wait()
                service._execute = execute
                service.runtime.terminate_owned = AsyncMock()
                operation = service._execute_with_lease_monitor(claim("status", {}), "", {}, None, Mock())
            task = asyncio.create_task(operation)
            await asyncio.wait_for(entered.wait(), 3)
            try:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(service.authority_workers.status()["in_flight"], 1)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(service.readiness.execution_available)
            finally:
                release.set()
                await service.close()

    async def test_watchdog_repeat_cancellation_joins_inspector(self):
        await self.service_worker_case("watchdog")

    async def test_admission_repeat_cancellation_joins_inspector(self):
        await self.service_worker_case("admission")

    async def test_active_monitor_repeat_cancellation_joins_inspector(self):
        await self.service_worker_case("active")

    async def barrier_case(self, *, failure=False, shutdown=False):
        entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        reasons = []
        workers = AuthorityWorkers(reasons.append)

        def check():
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(3):
                    raise RuntimeError("fixture barrier expired")
                if failure:
                    raise OSError("inspector gone")
                return 42
            finally:
                finished.set()

        task = asyncio.create_task(workers.run(check))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(workers.status()["in_flight"], 1)
        closing = asyncio.create_task(workers.close()) if shutdown else None
        try:
            if closing:
                closing.cancel()
                await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            if closing:
                try:
                    await closing
                except asyncio.CancelledError:
                    await workers.close()
            self.assertTrue(finished.is_set())
            self.assertEqual(workers.status()["in_flight"], 0)
            if failure:
                self.assertIn("authority_worker_failed", reasons)
        finally:
            release.set()
            await workers.close()

    async def test_repeat_cancellation_joins_worker(self):
        await self.barrier_case()

    async def test_direct_worker_task_cancellation_cannot_cancel_underlying_executor_future(self):
        workers = AuthorityWorkers(Mock())
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def check():
            loop.call_soon_threadsafe(entered.set)
            release.wait(3)

        caller = asyncio.create_task(workers.run(check))
        await entered.wait()
        worker = next(iter(workers._tasks))
        try:
            worker.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
            self.assertFalse(caller.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            await workers.close()

    async def test_worker_exception_during_cancelled_shutdown_is_owned(self):
        await self.barrier_case(failure=True, shutdown=True)

    async def test_serialized_workers_never_overlap(self):
        workers = AuthorityWorkers(Mock())
        active = 0
        maximum = 0

        def check():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.01)
            active -= 1

        await asyncio.gather(*(workers.run(check) for _ in range(4)))
        self.assertEqual(maximum, 1)
        await workers.close()
        with self.assertRaises(BrokerError):
            await workers.run(check)

    async def test_blocked_worker_reports_uncertainty_and_event_loop_remains_responsive(self):
        entered, release = asyncio.Event(), threading.Event()
        escalation = asyncio.Event()
        reasons = []
        loop = asyncio.get_running_loop()

        def failed(code):
            reasons.append(code)
            if code == "authority_worker_shutdown_unresolved":
                escalation.set()

        workers = AuthorityWorkers(failed)

        def check():
            loop.call_soon_threadsafe(entered.set)
            release.wait(3)

        task = asyncio.create_task(workers.run(check))
        await entered.wait()
        task.cancel()
        try:
            await asyncio.wait_for(escalation.wait(), 2)
            self.assertFalse(task.done())
            self.assertTrue(workers.status()["shutdown_unresolved"])
            self.assertIn("operator_intervention_required", reasons)
            self.assertEqual(workers.status()["in_flight"], 1)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await workers.close()

    async def test_service_shutdown_retains_registry_until_worker_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = BrokerService(config(Path(temporary) / "broker"))
            service.registry.directory_factory = None
            service.registry.storage_check = None
            service.registry.initialize(storage_verified=True, provision=True)
            descriptor = service.registry._state_directory().fd
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()

            def check():
                loop.call_soon_threadsafe(entered.set)
                release.wait(3)
                os.fstat(descriptor)

            worker = asyncio.create_task(service.authority_workers.run(check))
            await asyncio.wait_for(entered.wait(), 1)
            close = asyncio.create_task(service.close())
            try:
                with patch.object(service.registry, "close", wraps=service.registry.close) as registry_close:
                    await asyncio.sleep(0)
                    close.cancel()
                    await asyncio.sleep(0)
                    close.cancel()
                    await asyncio.sleep(0)
                    registry_close.assert_not_called()
                    self.assertFalse(close.done())
                    release.set()
                    await worker
                    with self.assertRaises(asyncio.CancelledError):
                        await close
                    registry_close.assert_called_once()
                await service.close()
                with self.assertRaises(BrokerError):
                    await service.start()
            finally:
                release.set()
                await service.close()

    async def test_cancelled_startup_joins_verifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = BrokerService(config(Path(temporary) / "broker"))
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()

            def check():
                loop.call_soon_threadsafe(entered.set)
                release.wait(3)
                return False

            service._registry_storage_verified = check
            startup = asyncio.create_task(service.start())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                startup.cancel()
                await asyncio.sleep(0)
                startup.cancel()
                await asyncio.sleep(0)
                self.assertFalse(startup.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await startup
                self.assertFalse(service.readiness.execution_available)
            finally:
                release.set()
                await service.close()


class FakeProcess:
    def __init__(self, *, kill_exits=True, kill_error=False, returncode=None):
        self.returncode = returncode
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exited = asyncio.Event()
        self.kill_called = asyncio.Event()
        self.wait_entered = asyncio.Event()
        self.kill_exits = kill_exits
        self.kill_error = kill_error
        if returncode is not None:
            self.exited.set()

    async def wait(self):
        self.wait_entered.set()
        await self.exited.wait()
        return self.returncode

    def kill(self):
        self.kill_called.set()
        if self.kill_error:
            raise OSError("kill failed")
        if self.kill_exits:
            self.finish(-9)

    def finish(self, returncode=0):
        self.returncode = returncode
        self.exited.set()


class ProcessDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reasons = []
        self.supervisor = ChildProcesses(self.reasons.append)
        self.process = FakeProcess()

    async def asyncTearDown(self):
        self.process.finish()
        await self.supervisor.close()

    async def run_process(self, timeout=0.12, reader=None):
        from synai.broker.runtime import _read_bounded
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=self.process)):
            async with asyncio.timeout(timeout + 0.5):
                return await self.supervisor.run(
                    "/fixture/podman", ("inspect",), {}, timeout, 1024, 1024,
                    reader or _read_bounded,
                )

    async def test_normal_exit(self):
        self.process.finish()
        self.assertEqual(await self.run_process(), (0, b"", b""))

    async def test_eof_before_exit_still_obeys_whole_deadline(self):
        start = asyncio.get_running_loop().time()
        with self.assertRaises(TimeoutError):
            await self.run_process()
        self.assertLess(asyncio.get_running_loop().time() - start, 0.3)
        self.assertTrue(self.process.kill_called.is_set())
        self.assertFalse(self.supervisor.children)

    async def test_blocked_stdout_and_stderr(self):
        async def read(stream, limit):
            await asyncio.Event().wait()

        with self.assertRaises(TimeoutError):
            await self.run_process(reader=read)
        self.assertTrue(self.process.kill_called.is_set())

    async def test_output_exhaustion(self):
        async def read(stream, limit):
            raise BrokerError("operation_output_limit", "Too large", 413)

        with self.assertRaises(BrokerError) as error:
            await self.run_process(reader=read)
        self.assertEqual(error.exception.code, "operation_output_limit")
        self.assertFalse(self.supervisor.children)

    async def test_stream_cancellation_does_not_abandon_child(self):
        async def reader(stream, limit):
            await asyncio.Event().wait()

        task = asyncio.create_task(self.run_process(timeout=0.5, reader=reader))
        await self.process.wait_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.supervisor.children)
        self.assertTrue(self.process.kill_called.is_set())

    async def test_output_failure_at_timeout_is_not_authoritative(self):
        async def reader(stream, limit):
            await asyncio.sleep(0.09)
            raise BrokerError("operation_output_limit", "Too large", 413)

        with self.assertRaises((BrokerError, TimeoutError)):
            await self.run_process(reader=reader)
        self.assertTrue(self.process.kill_called.is_set())

    async def test_unreaped_child_is_not_success(self):
        self.process.kill_exits = False
        start = asyncio.get_running_loop().time()
        with self.assertRaises(BrokerError) as error:
            await self.run_process()
        self.assertEqual(error.exception.code, "runtime_process_lifetime_unresolved")
        self.assertLess(asyncio.get_running_loop().time() - start, 0.3)
        self.assertTrue(self.supervisor.unresolved)
        self.assertIn(self.process, self.supervisor.children)

    async def test_termination_failure_is_unresolved(self):
        self.process.kill_error = True
        with self.assertRaises(BrokerError):
            await self.run_process()
        self.assertTrue(self.supervisor.unresolved)

    async def test_repeated_cancellation_during_cleanup_keeps_child_owned(self):
        self.process.kill_exits = False
        task = asyncio.create_task(self.run_process(timeout=0.5))
        await self.process.wait_entered.wait()
        task.cancel()
        await self.process.kill_called.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertIn(self.process, self.supervisor.children)
        self.process.finish(-9)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.supervisor.children)

    async def spawn_case(self, cancel):
        entered, release = asyncio.Event(), asyncio.Event()

        async def spawn(*arguments, **options):
            entered.set()
            await release.wait()
            return self.process

        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(self.supervisor.run(
                "/fixture/podman", (), {}, 0.08, 1024, 1024, AsyncMock(return_value=b""),
            ))
            await entered.wait()
            if cancel:
                task.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await task
                self.assertTrue(self.supervisor.unresolved)
                self.assertEqual(len(self.supervisor.spawns), 1)
            finally:
                release.set()
                await asyncio.gather(*tuple(self.supervisor.spawns))
                await asyncio.sleep(0)
        self.assertTrue(self.process.kill_called.is_set())

    async def test_spawn_timeout_retains_late_child_ownership(self):
        await self.spawn_case(False)

    async def test_cancelled_spawn_does_not_guess_pid(self):
        await self.spawn_case(True)

    async def test_no_production_subprocess_gate_is_relaxed(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = BrokerService(config(Path(temporary) / "broker"))
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                with self.assertRaises(BrokerError):
                    await service.runtime._run("info", timeout=0.1, maximum=1024)
            spawn.assert_not_awaited()
            await service.close()

    async def test_failed_logging_during_runtime_timeout_does_not_prevent_exact_child_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.selected = config(Path(temporary) / "broker")
            self.service = BrokerService(self.selected)
            try:
                with safety.SafetyTransitionTests.failing_logging(self) as handler, (
                    patch.object(self.service.runtime, "_require_storage_launch_acceptance")
                ), patch("synai.broker.runtime.shutil.which", return_value="/fixture/podman"), patch(
                    "asyncio.create_subprocess_exec", AsyncMock(return_value=self.process),
                ):
                    self.service._diagnostic_error("Runtime timeout handling")
                    with self.assertRaises(TimeoutError):
                        await self.service.runtime._run("inspect", timeout=0.12, maximum=1024)
                    self.assertTrue(handler.failed)
                self.assertTrue(self.process.kill_called.is_set())
                self.assertFalse(self.service.readiness.execution_available)
            finally:
                await self.service.close()

    async def test_runtime_nonzero_inspection_never_becomes_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = BrokerService(config(Path(temporary) / "broker"))
            self.process.finish(125)
            with (
                patch.object(service.runtime, "_require_storage_launch_acceptance"),
                patch("synai.broker.runtime.shutil.which", return_value="/fixture/podman"),
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=self.process)),
            ):
                with self.assertRaises(BrokerError) as failure:
                    await service.runtime._run("inspect", timeout=0.12, maximum=1024)
            self.assertEqual(failure.exception.code, "runtime_operation_failed")
            await service.close()

    async def test_unreaped_exists_check_never_reports_absence(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = BrokerService(config(Path(temporary) / "broker"))
            with (
                patch.object(service.runtime, "_require_storage_launch_acceptance"),
                patch("synai.broker.runtime.shutil.which", return_value="/fixture/podman"),
                patch.object(service.runtime.child_processes, "run", side_effect=BrokerError(
                    "runtime_process_lifetime_unresolved", "Unresolved", 503,
                )),
            ):
                with self.assertRaises(BrokerError):
                    await service.runtime._container_exists("a" * 64)
            await service.close()

    async def test_shutdown_with_unreaped_child_retains_resources_and_refuses_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = BrokerService(config(Path(temporary) / "broker"))
            self.process.kill_exits = False
            service.runtime.child_processes.children.add(self.process)
            try:
                with patch.object(service.registry, "close") as closed:
                    await service.close()
                    closed.assert_not_called()
                self.assertTrue(service.runtime.processes_unresolved)
                self.assertIn("operator_intervention_required", service.readiness.reasons)
                with self.assertRaises(BrokerError):
                    await service.start()
            finally:
                self.process.finish()
                await service.runtime.close_processes()
                service.registry.close()
