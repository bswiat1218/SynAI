from __future__ import annotations

import asyncio
import tempfile
import socket
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import uvicorn

from synai.broker import cli
from synai.broker.app import BrokerService, create_app
from synai.broker.config import BrokerReadiness
from synai.broker.protocol import BrokerError
from synai.broker.registry import RegistryTrust
from synai.broker.runtime import ContainmentResult
from tests.test_broker import FakeQuotaManager, config


class BrokerCliLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = replace(
            config(Path(self.temporary.name) / "broker"),
            storage_watchdog_interval_seconds=0.1,
        )
        self.service = BrokerService(self.config)
        self.service.quota_manager = FakeQuotaManager(self.config, self.service.registry)
        self.service.registry.initialize(storage_verified=True, provision=True)
        self.service.runtime.verify_startup = AsyncMock(
            return_value=BrokerReadiness(False, False, {}, ("runtime_unaccepted",)),
        )
        self.logging = Mock()
        self.logging_patch = patch.object(cli, "_configure_bounded_logging", self.logging)
        self.logging_patch.start()
        self.app = None

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.logging_patch.stop()
        self.temporary.cleanup()

    def cli_app(self):
        with (
            patch.object(cli.BrokerConfig, "from_env", return_value=self.config),
            patch.object(cli, "BrokerService", return_value=self.service),
            patch.object(cli, "_configure_bounded_logging", self.logging),
            patch.object(cli.uvicorn, "run") as run,
        ):
            cli.main()
        self.assertFalse(self.service._started)
        self.assertIsNone(self.service._storage_watchdog_task)
        self.assertEqual(run.call_args.kwargs["lifespan"], "on")
        self.app = run.call_args.args[0]
        return self.app

    async def wait_for(self, predicate) -> None:
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_cli_lifespan_watchdog_and_http_share_loop_and_idle_loss_is_detected(self) -> None:
        app = self.cli_app()
        ticks = 0
        unhealthy = False

        def inspect():
            nonlocal ticks
            ticks += 1
            return (False, ("storage_role_expired",)) if unhealthy else (True, ())

        self.service.quota_manager.verify_startup = Mock(side_effect=inspect)
        async with app.router.lifespan_context(app):
            self.logging.assert_called_once()
            task = self.service._storage_watchdog_task
            self.assertIs(task.get_loop(), asyncio.get_running_loop())
            self.assertIs(self.service._lifecycle_loop, asyncio.get_running_loop())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://broker",
            ) as client:
                before = ticks
                await self.wait_for(lambda: ticks >= before + 2)
                self.assertEqual((await client.get("/health")).json(), {"status": "alive"})
                unhealthy = True
                await self.wait_for(lambda: "storage_role_expired" in self.service.readiness.reasons)
                response = await client.get("/v1/ready")
                self.assertFalse(response.json()["execution_available"])
                unhealthy = False
                await self.wait_for(lambda: ticks >= before + 4)
                self.assertFalse(self.service.readiness.execution_available)
        self.assertTrue(task.done())
        self.assertIsNone(self.service._storage_watchdog_task)
        self.assertTrue(self.service._closed)

    async def test_cli_created_application_under_real_uvicorn_keeps_idle_watchdog_alive(self) -> None:
        app = self.cli_app()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, lifespan="on",
            log_config=None, access_log=False,
        ))
        # No host service or TLS material is provisioned. Cleartext must still be denied.
        with patch.object(server, "capture_signals"):
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            watchdog = None
            try:
                await self.wait_for(lambda: server.started or serving.done())
                self.assertFalse(serving.done(), "Uvicorn failed before lifespan startup")
                watchdog = self.service._storage_watchdog_task
                self.assertIs(watchdog.get_loop(), serving.get_loop())
                self.service.quota_manager.verify_startup = Mock(return_value=(True, ()))
                await self.wait_for(lambda: self.service.quota_manager.verify_startup.call_count >= 2)
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                self.assertEqual(response.status_code, 426)
                self.service.quota_manager.verify_startup.return_value = (False, ("inspector_unavailable",))
                await self.wait_for(lambda: "inspector_unavailable" in self.service.readiness.reasons)
                self.assertFalse(self.service.readiness.execution_available)
                self.assertFalse(watchdog.done())
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, timeout=5)
                listener.close()
        self.assertTrue(watchdog.done())
        self.assertIsNone(self.service._storage_watchdog_task)

    async def test_startup_barrier_prevents_dispatch_and_logging_until_checks_finish(self) -> None:
        app = self.cli_app()
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.service.start

        async def start():
            entered.set()
            await release.wait()
            await original()

        self.service.start = start
        lifespan = app.router.lifespan_context(app)
        startup = asyncio.create_task(lifespan.__aenter__())
        await entered.wait()
        self.logging.assert_not_called()
        with self.assertRaises(BrokerError) as denied:
            await self.service.dispatch(None)
        self.assertEqual(denied.exception.code, "broker_not_ready")
        release.set()
        await startup
        await lifespan.__aexit__(None, None, None)

    async def test_inspector_exception_and_watchdog_cancellation_remain_fail_closed(self) -> None:
        app = self.cli_app()
        async with app.router.lifespan_context(app):
            self.service.quota_manager.verify_startup = Mock(side_effect=RuntimeError("helper crashed"))
            await self.wait_for(lambda: "storage_watchdog_failed" in self.service.readiness.reasons)
            task = self.service._storage_watchdog_task
            task.cancel()
            await self.wait_for(lambda: self.service._storage_watchdog_task is not task)
            replacement = self.service._storage_watchdog_task
            self.assertIs(replacement.get_loop(), asyncio.get_running_loop())
            self.assertIn("storage_watchdog_stopped", self.service.readiness.reasons)
            self.assertFalse(self.service.readiness.execution_available)
        self.assertTrue(replacement.done())
        self.assertIsNone(self.service._storage_watchdog_task)

    async def test_shutdown_joins_independent_containment(self) -> None:
        app = self.cli_app()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def contain(_):
            entered.set()
            await release.wait()
            return ContainmentResult("runtime_unavailable", error_code="runtime_unavailable")

        self.service.runtime.contain_recorded = AsyncMock(side_effect=contain)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        task = self.service._containment_task("a" * 32, durable_quarantine=False)
        await entered.wait()
        watchdog = self.service._storage_watchdog_task
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await self.wait_for(lambda: watchdog.done())
        self.assertFalse(shutdown.done())
        self.assertFalse(task.cancelled())
        release.set()
        await shutdown
        self.assertTrue(task.done())
        self.assertFalse(self.service.readiness.execution_available)

    async def test_shutdown_joins_inflight_inspector_worker_before_closing_registry(self) -> None:
        app = self.cli_app()
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()

        def check():
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("test inspector barrier timed out")
            finished.set()
            return True, ()

        self.service.quota_manager.verify_startup = Mock(side_effect=check)
        await self.wait_for(entered.is_set)
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        try:
            await self.wait_for(lambda: self.service._closed)
            self.assertFalse(shutdown.done())
            self.assertIsNotNone(self.service.registry._directory)
        finally:
            release.set()
            await shutdown
        self.assertTrue(finished.is_set())
        self.assertIsNone(self.service.registry._directory)
        self.assertIsNone(self.service._storage_watchdog_task)

    async def test_cli_logging_failure_aborts_lifespan_and_joins_watchdog(self) -> None:
        app = self.cli_app()
        self.logging.side_effect = RuntimeError("bounded log storage unavailable")
        with self.assertRaises(RuntimeError):
            async with app.router.lifespan_context(app):
                self.fail("Startup failure must not serve requests")
        self.assertTrue(self.service._closed)
        self.assertIsNone(self.service._storage_watchdog_task)
        self.assertFalse(self.service.readiness.execution_available)

    async def test_duplicate_application_and_closed_instance_cannot_restart_watchdog(self) -> None:
        app = self.cli_app()
        async with app.router.lifespan_context(app):
            watchdog = self.service._storage_watchdog_task
            other = create_app(self.service)
            with self.assertRaises(BrokerError):
                async with other.router.lifespan_context(other):
                    self.fail("Duplicate lifecycle admitted")
            self.assertIs(self.service._storage_watchdog_task, watchdog)
        with self.assertRaises(BrokerError):
            async with app.router.lifespan_context(app):
                self.fail("Closed instance reused")
        self.assertIsNone(self.service._storage_watchdog_task)

    async def test_fresh_instance_restart_runs_registry_recovery_and_preserves_gates(self) -> None:
        app = self.cli_app()
        async with app.router.lifespan_context(app):
            self.assertEqual(self.service.registry.trust, RegistryTrust.COMPLETE)
        fresh = BrokerService(self.config)
        fresh.quota_manager = FakeQuotaManager(self.config, fresh.registry)
        fresh.runtime.verify_startup = self.service.runtime.verify_startup
        original = fresh.recover
        fresh.recover = AsyncMock(wraps=original)
        app = create_app(fresh)
        async with app.router.lifespan_context(app):
            fresh.recover.assert_awaited_once()
            self.assertFalse(fresh.readiness.execution_available)
            self.assertIn("external_filesystem_resolution_unaccepted", fresh.readiness.reasons)
            self.assertFalse(fresh._storage_watchdog_task.done())
        self.assertIsNone(fresh._storage_watchdog_task)


class BrokerCrossLoopTests(unittest.TestCase):
    def test_service_started_on_temporary_loop_is_refused_on_serving_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = config(Path(temporary) / "broker")
            service = BrokerService(selected)
            service.quota_manager = FakeQuotaManager(selected, service.registry)
            service.registry.initialize(storage_verified=True, provision=True)
            service.runtime.verify_startup = AsyncMock(
                return_value=BrokerReadiness(False, False, {}, ("unaccepted",)),
            )

            async def bootstrap():
                await service.start()
                # Simulate a foreign bootstrap without leaving an orphan at loop close.
                watchdog = service._storage_watchdog_task
                service._storage_watchdog_task = None
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)

            asyncio.run(bootstrap())

            async def serve():
                with self.assertRaises(BrokerError) as denied:
                    await service.start()
                self.assertEqual(denied.exception.code, "service_lifecycle_invalid")
                await service.close()

            asyncio.run(serve())
            self.assertFalse(service.readiness.execution_available)
