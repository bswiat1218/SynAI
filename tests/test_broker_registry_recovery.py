from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from synai.broker.app import BrokerService, OperationRequest
from synai.broker.config import BrokerReadiness
from synai.broker.protocol import BrokerError, sign_claim
from synai.broker.registry import BrokerRegistry, RegistryTrust
from synai.broker.runtime import ContainmentResult
from tests.test_broker import CLAIM_PRIVATE, FakeQuotaManager, claim, config


class RegistryTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.selected = config(Path(self.temporary.name) / "broker")
        self.registry = BrokerRegistry(self.selected)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provision(self) -> None:
        self.registry.initialize(storage_verified=True, provision=True)

    def test_first_initialization_requires_explicit_storage_and_operator_approval(self) -> None:
        with self.assertRaises(BrokerError):
            self.registry.initialize()
        self.assertFalse(self.registry.path.exists())
        self.registry = BrokerRegistry(self.selected)
        with self.assertRaises(BrokerError):
            self.registry.initialize(storage_verified=True)
        self.assertFalse(self.registry.path.exists())
        self.registry = BrokerRegistry(self.selected)
        self.provision()
        self.assertEqual(self.registry.trust, RegistryTrust.RECOVERABLE)
        self.assertTrue(self.registry.seal_path.is_file())

    def test_missing_historical_database_is_never_recreated_even_by_provisioning(self) -> None:
        self.provision()
        self.registry.path.unlink()
        for _ in range(2):
            restarted = BrokerRegistry(self.selected)
            with self.assertRaises(BrokerError):
                restarted.initialize(storage_verified=True, provision=True)
            self.assertEqual(restarted.trust, RegistryTrust.UNAVAILABLE)
            self.assertFalse(restarted.path.exists())

    def test_missing_identity_evidence_requires_offline_adoption(self) -> None:
        self.provision()
        self.registry.seal_path.unlink()
        restarted = BrokerRegistry(self.selected)
        original = restarted.path.read_bytes()
        with self.assertRaises(BrokerError):
            restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.path.read_bytes(), original)
        self.assertFalse(restarted.seal_path.exists())

    def test_unsafe_legacy_ownership_is_rejected_before_any_database_write(self) -> None:
        self.registry.path.write_bytes(b"unverified historical state")
        os.chmod(self.registry.path, 0o644)
        with self.assertRaises(BrokerError):
            self.registry.initialize(storage_verified=True, adopt_legacy=True)
        self.assertEqual(self.registry.path.read_bytes(), b"unverified historical state")
        self.assertFalse(self.registry.dirty_path.exists())
        self.assertFalse(self.registry.seal_path.exists())

    def test_replacement_and_policy_changes_revoke_authority(self) -> None:
        self.provision()
        old = self.registry.path.with_suffix(".old")
        self.registry.path.rename(old)
        self.registry.path.write_bytes(old.read_bytes())
        os.chmod(self.registry.path, 0o600)
        with self.assertRaises(BrokerError):
            self.registry.task("1" * 32)
        self.assertEqual(self.registry.trust, RegistryTrust.MISMATCH)
        self.registry.path.unlink()
        old.rename(self.registry.path)
        restarted = BrokerRegistry(replace(self.selected, claim_key=b"x" * 32))
        with self.assertRaises(BrokerError):
            restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.trust, RegistryTrust.MISMATCH)

    def test_corrupt_database_never_passes_integrity_or_creates_empty_schema(self) -> None:
        self.provision()
        self.registry.path.write_bytes(b"not a SQLite database")
        # Simulate operator-supplied identity evidence without asserting integrity.
        self.registry._seal()
        restarted = BrokerRegistry(self.selected)
        with self.assertRaises(BrokerError):
            restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.trust, RegistryTrust.CORRUPT)
        self.assertEqual(restarted.path.read_bytes(), b"not a SQLite database")

    def test_missing_schema_is_not_reconstructed(self) -> None:
        self.provision()
        with self.registry.connect() as connection:
            connection.execute("DROP TABLE broker_request_nonces")
        restarted = BrokerRegistry(self.selected)
        with self.assertRaises(BrokerError):
            restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.trust, RegistryTrust.CORRUPT)

    def test_unreadable_database_does_not_become_empty_history(self) -> None:
        self.provision()
        original = self.registry.path.read_bytes()
        restarted = BrokerRegistry(self.selected)
        from synai.broker.filesystem import Directory
        real_open = Directory.open

        def denied(directory: Directory, name: str, *args: object, **kwargs: object) -> object:
            if directory.path == restarted.path.parent and name == restarted.path.name:
                raise PermissionError("injected unreadable registry")
            return real_open(directory, name, *args, **kwargs)

        with patch.object(Directory, "open", denied):
            with self.assertRaises(PermissionError):
                restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.trust, RegistryTrust.UNAVAILABLE)
        self.assertEqual(restarted.path.read_bytes(), original)

    def test_recovery_incomplete_blocks_new_authority_and_slot_reuse(self) -> None:
        self.provision()
        self.registry.fail(RegistryTrust.RECOVERY_INCOMPLETE, "recovery_incomplete")
        request = claim("prepare", {})
        with self.assertRaises(BrokerError):
            self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        with self.assertRaises(BrokerError):
            self.registry.release_slot(request.task_id, "not evidence")
        self.assertEqual(self.registry.container_records(), ())

    def test_unexpected_sidecars_and_interruption_require_operator_recovery(self) -> None:
        self.provision()
        for name in ("broker.sqlite3-wal", "broker.sqlite3-shm",
                     "broker.sqlite3-journal", "registry-interrupted"):
            with self.subTest(name=name):
                path = self.selected.state_dir / name
                path.write_bytes(b"uncertain evidence")
                restarted = BrokerRegistry(self.selected)
                with self.assertRaises(BrokerError):
                    restarted.initialize(storage_verified=True)
                self.assertEqual(restarted.trust, RegistryTrust.RECOVERY_INCOMPLETE)
                self.assertEqual(path.read_bytes(), b"uncertain evidence")
                path.unlink()

    def test_inconsistent_container_ownership_blocks_recovery(self) -> None:
        self.provision()
        request = claim("prepare", {})
        self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.registry.transition(request.task_id, "ready")
        self.registry.reserve_container(request.task_id, request.allocation_id)
        self.registry.bind_container(request.task_id, request.allocation_id, "c" * 64)
        with self.registry.connect() as connection:
            connection.execute("UPDATE broker_containers SET snapshot_digest = ?", ("d" * 64,))
        restarted = BrokerRegistry(self.selected)
        with self.assertRaises(BrokerError):
            restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.trust, RegistryTrust.MISMATCH)

    def test_quota_exhaustion_transaction_preserves_interruption_evidence(self) -> None:
        self.provision()
        with self.assertRaises(BrokerError):
            with self.registry.connect():
                raise sqlite3.OperationalError("database or disk is full")
        self.assertEqual(self.registry.trust, RegistryTrust.UNAVAILABLE)
        self.assertTrue(self.registry.dirty_path.exists())
        restarted = BrokerRegistry(self.selected)
        with self.assertRaises(BrokerError):
            restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.trust, RegistryTrust.RECOVERY_INCOMPLETE)

    def test_commit_rollback_and_checkpoint_failures_are_not_success(self) -> None:
        real_connect = sqlite3.connect
        for failure in ("commit", "rollback", "checkpoint"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                selected = config(Path(directory) / "broker")
                registry = BrokerRegistry(selected)
                registry.initialize(storage_verified=True, provision=True)

                class FailingConnection(sqlite3.Connection):
                    def commit(self) -> None:
                        if failure == "commit":
                            raise sqlite3.OperationalError("injected commit failure")
                        super().commit()

                    def rollback(self) -> None:
                        if failure == "rollback":
                            raise sqlite3.OperationalError("injected rollback failure")
                        super().rollback()

                    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
                        if failure == "checkpoint" and "wal_checkpoint" in sql:
                            raise sqlite3.OperationalError("injected checkpoint failure")
                        return super().execute(sql, parameters)

                def open_fault(*args: object, **kwargs: object) -> sqlite3.Connection:
                    return real_connect(*args, **kwargs, factory=FailingConnection)

                with patch("synai.broker.registry.sqlite3.connect", side_effect=open_fault):
                    with self.assertRaises(BrokerError):
                        if failure == "rollback":
                            with registry.connect() as connection:
                                connection.execute("BEGIN IMMEDIATE")
                                connection.rollback()
                        else:
                            registry.consume_request_nonce("f" * 32, 100)
                self.assertTrue(registry.dirty_path.exists())
                self.assertEqual(registry.trust, RegistryTrust.UNAVAILABLE)

    def test_missing_active_wal_or_shm_is_unresolved(self) -> None:
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                registry = BrokerRegistry(config(Path(directory) / "broker"))
                registry.initialize(storage_verified=True, provision=True)
                with self.assertRaises(BrokerError):
                    with registry.connect():
                        registry.path.with_name(registry.path.name + suffix).unlink()
                self.assertEqual(registry.trust, RegistryTrust.RECOVERY_INCOMPLETE)

    def test_crash_after_allocation_preserves_uncertain_ownership_without_reuse(self) -> None:
        self.provision()
        request = claim("prepare", {})
        self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.registry.transition(request.task_id, "ready")
        self.registry.reserve_container(request.task_id, request.allocation_id)
        restarted = BrokerRegistry(self.selected)
        restarted.initialize(storage_verified=True)
        record = restarted.container_record(request.task_id)
        self.assertIsNone(record["container_id"])
        self.assertEqual(record["lifecycle_state"], "quarantined")
        self.assertTrue(restarted.has_quarantined_slots())


class RegistryServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.selected = config(Path(self.temporary.name) / "broker")
        self.service = BrokerService(self.selected)
        self.service.quota_manager = FakeQuotaManager(self.selected, self.service.registry)

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.temporary.cleanup()

    def provision(self) -> None:
        self.service.registry.initialize(storage_verified=True, provision=True)

    async def test_startup_missing_registry_escalates_without_runtime_discovery(self) -> None:
        self.service.runtime.verify_startup = AsyncMock()
        self.service.runtime.contain_recorded = AsyncMock()
        await asyncio.gather(self.service.start(), self.service.start())
        self.assertFalse(self.service.registry.path.exists())
        self.assertFalse(self.service.readiness.execution_available)
        self.assertTrue(self.service.registry.status()["operator_intervention_required"])
        self.service.runtime.verify_startup.assert_not_awaited()
        self.service.runtime.contain_recorded.assert_not_awaited()

    async def test_inspector_outage_does_not_write_or_replace_existing_state(self) -> None:
        self.provision()
        original = self.service.registry.path.read_bytes()
        self.service.quota_manager.verify_storage_role = Mock(return_value=False)
        await self.service.start()
        self.assertEqual(self.service.registry.path.read_bytes(), original)
        self.assertEqual(self.service.registry.trust, RegistryTrust.STORAGE_UNVERIFIED)
        self.assertFalse(self.service.readiness.execution_available)

    async def test_active_state_loss_revokes_readiness_and_refuses_speculative_containment(self) -> None:
        self.provision()
        self.service._started = True
        self.service.readiness = BrokerReadiness(True, True, {}, ())
        self.service.quota_manager.verify_storage_role = Mock(return_value=False)
        with self.assertRaises(BrokerError):
            self.service.registry.consume_request_nonce("f" * 32, 100)
        self.assertFalse(self.service.readiness.execution_available)
        self.service.runtime.emergency_containment_available = Mock(return_value=True)
        self.service.runtime._run = AsyncMock()
        outcome = await self.service.runtime.contain_recorded("1" * 32)
        self.assertFalse(outcome.verified)
        self.service.runtime._run.assert_not_awaited()
        request = claim("prepare", {})
        with self.assertRaises(BrokerError):
            await self.service.dispatch(OperationRequest(
                schema_version=1, claim=request.to_dict(),
                claim_signature=sign_claim(request, CLAIM_PRIVATE),
                kind="prepare", arguments={},
            ))
        self.service.quota_manager.verify_storage_role = Mock(return_value=True)
        await self.service._handle_storage_assurance_loss(("registry_unavailable",))
        self.assertFalse(self.service.readiness.execution_available)

    async def test_containment_evidence_write_failure_is_unresolved_and_blocks_restart(self) -> None:
        self.provision()
        request = claim("prepare", {})
        registry = self.service.registry
        registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        registry.transition(request.task_id, "ready")
        registry.reserve_container(request.task_id, request.allocation_id)
        registry.bind_container(request.task_id, request.allocation_id, "c" * 64)
        self.service.readiness = BrokerReadiness(True, True, {}, ())
        self.service.runtime.contain_recorded = AsyncMock(return_value=ContainmentResult(
            "verified_stopped", evidence="e" * 64,
        ))
        with patch.object(registry, "_seal", side_effect=OSError("injected seal failure")):
            outcome = await self.service._contain_and_persist(request.task_id, durable_quarantine=True)
        self.assertFalse(outcome.verified)
        self.assertFalse(self.service.readiness.execution_available)
        restarted = BrokerRegistry(self.selected)
        with self.assertRaises(BrokerError):
            restarted.initialize(storage_verified=True)
        self.assertEqual(restarted.trust, RegistryTrust.RECOVERY_INCOMPLETE)

    async def test_recovery_cancellation_never_claims_complete_or_restores_readiness(self) -> None:
        self.provision()
        entered = asyncio.Event()

        async def recover() -> None:
            entered.set()
            await asyncio.Event().wait()

        self.service._recover = recover
        self.service.readiness = BrokerReadiness(True, True, {}, ())
        task = asyncio.create_task(self.service.recover())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.service.registry.trust, RegistryTrust.RECOVERY_INCOMPLETE)
        self.assertFalse(self.service.readiness.execution_available)

    async def test_concurrent_watchdog_and_recovery_loss_remain_latched(self) -> None:
        self.provision()
        self.service.readiness = BrokerReadiness(True, True, {}, ())
        self.service.quota_manager.verify_storage_role = Mock(return_value=False)
        self.service._start_storage_watchdog()
        with self.assertRaises(BrokerError):
            await asyncio.gather(self.service.recover(), self.service.recover())
        self.service.quota_manager.verify_storage_role = Mock(return_value=True)
        await self.service._handle_storage_assurance_loss(("registry_unavailable",))
        self.assertFalse(self.service.readiness.execution_available)
        self.assertTrue(self.service.registry.status()["operator_intervention_required"])

    async def test_uncertain_tasks_survive_repeated_restart_without_reuse(self) -> None:
        self.provision()
        request = claim("prepare", {})
        self.service.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        for _ in range(2):
            registry = BrokerRegistry(self.selected)
            registry.initialize(storage_verified=True)
            self.assertEqual(registry.task(request.task_id)["state"], "uncertain")
            self.assertTrue(registry.has_quarantined_slots())
            self.assertEqual(registry.uncertain_operations()[0]["operation_id"], request.operation_id)

    async def test_successful_fresh_start_requires_reconciliation_and_keeps_other_gates(self) -> None:
        self.provision()
        self.service.runtime.verify_startup = AsyncMock(
            return_value=BrokerReadiness(False, False, {}, ("runtime_acceptance_missing",)),
        )
        await self.service.start()
        self.assertEqual(self.service.registry.trust, RegistryTrust.COMPLETE)
        self.assertFalse(self.service.readiness.execution_available)
        self.assertIn("runtime_acceptance_missing", self.service.readiness.reasons)
