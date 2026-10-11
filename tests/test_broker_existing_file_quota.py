from __future__ import annotations

import errno
import hashlib
import logging
import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from synai.broker.cli import _configure_bounded_logging
from synai.broker.app import BrokerService
from synai.broker.filesystem import Directory, write_new
from synai.broker.operations import execute_file_operation
from synai.broker.protocol import BrokerError
from synai.broker.quota_slots import QuotaSlotManager, _Mount, aggregate_storage_reasons
from synai.broker.registry import BrokerRegistry
from synai.broker.storage_inspector import InspectorUnavailable
from tests.test_broker import config
from tests.test_broker_runtime_storage import quota_observations


class ExistingFileQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.selected = config(Path(self.temporary.name) / "broker", slot_count=4)
        self.roles = {role.role_id: role for role in self.selected.storage_roles}
        self.observations = quota_observations(self.selected)
        self.registry = BrokerRegistry(self.selected)
        self.manager = QuotaSlotManager(
            self.selected, self.registry,
            Mock(inspect=lambda role_id: self.observations[role_id]),
        )
        self.role = next(role for role in self.selected.storage_roles if role.kind == "broker_state")
        self.projects: dict[int, int] = {}
        self.new_project: int | None = None
        self.attributes = patch("synai.broker.quota_slots._project_attributes", side_effect=self.project)
        self.mounts = patch("synai.broker.quota_slots._mount_for_fd", side_effect=self.mount)
        self.devices = patch("synai.broker.quota_slots._verify_filesystem_device")
        self.attributes.start()
        self.mounts.start()
        self.devices.start()
        self.handlers = list(logging.getLogger().handlers)
        self.level = logging.getLogger().level
        self.log_handler = None

    def tearDown(self):
        if self.log_handler is not None:
            self.log_handler.close()
        logging.getLogger().handlers[:] = self.handlers
        logging.getLogger().setLevel(self.level)
        self.registry.close()
        self.attributes.stop()
        self.mounts.stop()
        self.devices.stop()
        self.temporary.cleanup()

    def project(self, descriptor):
        inode = os.fstat(descriptor).st_ino
        return self.projects.get(inode, self.new_project or self.role.project_id), 0

    def mount(self, descriptor):
        return _Mount(
            10, "8:2", "/", str(self.role.mount_path),
            frozenset({"prjquota"}), "ext4", frozenset({"prjquota"}),
        )

    def authority(self):
        return Directory(
            self.role.path, owner_uid=self.role.owner_uid, owner_gid=self.role.owner_gid,
            private=True,
            file_verifier=lambda descriptor: self.manager.verify_persistent_file(self.role.role_id, descriptor),
            file_failure_callback=lambda code: self.manager._persistent_file_failed(self.role.role_id, code),
        )

    def file(self, name="existing", data=b"historical"):
        path = self.role.path / name
        path.write_bytes(data)
        path.chmod(0o600)
        self.projects[path.stat().st_ino] = self.role.project_id
        return path

    def charged_registry(self):
        self.registry.directory_factory = self.authority
        self.registry.initialize(storage_verified=True, provision=True)
        for path in self.role.path.iterdir():
            if path.is_file():
                self.projects[path.stat().st_ino] = self.role.project_id

    def test_correct_file_project_without_directory_inheritance_flag_is_valid(self):
        path = self.file()
        self.projects[path.stat().st_ino] = self.role.project_id
        with self.authority() as directory:
            descriptor = directory.open(path.name, os.O_WRONLY | os.O_APPEND)
            os.write(descriptor, b"+")
            directory.verify_file(path.name, descriptor)
            os.close(descriptor)
        self.assertEqual(path.read_bytes(), b"historical+")

    def test_legacy_zero_project_and_wrong_project_are_refused_without_repair(self):
        path = self.file()
        for project in (0, self.role.project_id + 1):
            self.projects[path.stat().st_ino] = project
            for _ in range(2):
                with self.authority() as directory:
                    with self.assertRaises(BrokerError):
                        directory.open(path.name, os.O_WRONLY | os.O_APPEND)
                self.assertEqual(path.read_bytes(), b"historical")
                self.assertEqual(self.projects[path.stat().st_ino], project)

    def test_new_file_project_is_verified_before_first_byte(self):
        self.new_project = self.role.project_id + 1
        with self.authority() as directory:
            with self.assertRaises(BrokerError):
                write_new(directory, "new", b"must not persist")
        self.assertEqual((self.role.path / "new").read_bytes(), b"")

    def test_wrong_owner_permissions_and_hardlinks_are_refused(self):
        path = self.file()
        path.chmod(0o644)
        with self.authority() as directory:
            with self.assertRaises(BrokerError):
                directory.open(path.name, os.O_WRONLY)
        path.chmod(0o600)
        os.link(path, self.role.path / "alias")
        with self.authority() as directory:
            with self.assertRaises(BrokerError):
                directory.open(path.name, os.O_WRONLY)
        (self.role.path / "alias").unlink()
        with self.authority() as directory:
            descriptor = os.open(path, os.O_RDONLY)
            info = os.fstat(descriptor)
            altered = list(info)
            altered[4] = self.role.owner_uid + 1
            with patch("synai.broker.quota_slots.os.fstat", return_value=os.stat_result(altered)):
                with self.assertRaises(BrokerError):
                    self.manager.verify_persistent_file(self.role.role_id, descriptor)
            os.close(descriptor)

    def test_symlink_special_file_and_mount_redirection_are_refused(self):
        path = self.file()
        (self.role.path / "link").symlink_to(path)
        os.mkfifo(self.role.path / "fifo", 0o600)
        with self.authority() as directory:
            for name in ("link", "fifo"):
                with self.assertRaises((BrokerError, OSError)):
                    directory.open(name, os.O_RDONLY | os.O_NONBLOCK)
            for changes in ({"mount_id": 11}, {"filesystem": "xfs"}, {"target": "/"}, {"device": "8:99"}):
                mount = replace(self.mount(directory.fd), **changes)
                with patch("synai.broker.quota_slots._mount_for_fd", return_value=mount):
                    if "device" in changes:
                        with patch("synai.broker.quota_slots._verify_filesystem_device", side_effect=ValueError("wrong UUID")):
                            with self.assertRaises(BrokerError):
                                directory.open(path.name, os.O_RDONLY)
                    else:
                        with self.assertRaises(BrokerError):
                            directory.open(path.name, os.O_RDONLY)

    def test_inode_substitution_after_open_revokes_file_authority(self):
        path = self.file()
        with self.authority() as directory:
            descriptor = directory.open(path.name, os.O_WRONLY | os.O_APPEND)
            path.rename(self.role.path / "saved")
            self.file()
            with self.assertRaises(BrokerError):
                directory.verify_file(path.name, descriptor)
            os.close(descriptor)
            with self.assertRaises(BrokerError):
                directory.open(path.name, os.O_WRONLY)
        self.assertEqual(path.read_bytes(), b"historical")

    def test_truncate_is_rejected_before_data_changes(self):
        path = self.file()
        with self.authority() as directory:
            with self.assertRaises(BrokerError):
                directory.open(path.name, os.O_WRONLY | os.O_TRUNC)
        self.assertEqual(path.read_bytes(), b"historical")

    def test_verified_atomic_replacement_updates_only_exact_identity(self):
        self.file("target")
        with self.authority() as directory:
            descriptor = directory.open("target", os.O_RDONLY)
            os.close(descriptor)
            write_new(directory, "pending", b"new")
            directory.replace_file("pending", "target")
            descriptor = directory.open("target", os.O_WRONLY | os.O_APPEND)
            os.close(descriptor)
        self.assertEqual((self.role.path / "target").read_bytes(), b"new")

    def test_atomic_replacement_wrong_project_preserves_existing_evidence(self):
        path = self.file("target")
        with self.authority() as directory:
            write_new(directory, "pending", b"new")
            self.projects[(self.role.path / "pending").stat().st_ino] = 0
            with self.assertRaises(BrokerError):
                directory.replace_file("pending", "target")
        self.assertEqual(path.read_bytes(), b"historical")
        self.assertEqual((self.role.path / "pending").read_bytes(), b"new")

    def test_project_change_at_postreplacement_boundary_is_refused(self):
        path = self.file("target")
        real_replace = os.replace
        def substitute(*args, **kwargs):
            real_replace(*args, **kwargs)
            self.projects[path.stat().st_ino] = 0
        with self.authority() as directory:
            write_new(directory, "pending", b"new")
            with patch("synai.broker.filesystem.os.replace", side_effect=substitute):
                with self.assertRaises(BrokerError):
                    directory.replace_file("pending", "target")
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual(self.projects[path.stat().st_ino], 0)

    def test_existing_and_new_workspace_files_use_same_file_authority(self):
        path = self.file("workspace")
        with self.authority() as directory:
            execute_file_operation(directory, "write_file", {
                "path": path.name, "content": "new",
                "expected_sha256": hashlib.sha256(b"historical").hexdigest(),
            }, 1024)
            self.projects[path.stat().st_ino] = self.role.project_id
            self.new_project = 1
            with self.assertRaises(BrokerError):
                execute_file_operation(directory, "write_file", {
                    "path": path.name, "content": "rejected",
                    "expected_sha256": hashlib.sha256(b"new").hexdigest(),
                }, 1024)
        self.assertEqual(path.read_bytes(), b"new")
        self.assertTrue(any(name.startswith(".synai-") for name in os.listdir(self.role.path)))

    def test_recursive_inventory_checks_arbitrary_persistent_files(self):
        nested = self.role.path / "audit"
        nested.mkdir(mode=0o700)
        path = nested / "record"
        path.write_bytes(b"audit")
        path.chmod(0o600)
        self.projects[path.stat().st_ino] = 0
        with patch.object(self.manager, "open_storage_role", side_effect=lambda _: self.authority()):
            with self.assertRaises(BrokerError):
                self.manager.verify_existing_files(self.role.role_id)
        self.assertEqual(path.read_bytes(), b"audit")

    def test_database_project_mismatch_refuses_restart_without_replacement(self):
        self.charged_registry()
        original = self.registry.path.read_bytes()
        self.projects[self.registry.path.stat().st_ino] = 0
        for _ in range(2):
            restarted = BrokerRegistry(self.selected)
            restarted.directory_factory = self.authority
            try:
                with self.assertRaises(BrokerError):
                    restarted.initialize(storage_verified=True)
                self.assertIsNotNone(restarted.failure_code)
            finally:
                restarted.close()
        self.assertEqual(self.registry.path.read_bytes(), original)

    def test_db_sidecars_seals_and_markers_check_actual_project(self):
        self.charged_registry()
        for suffix in ("-wal", "-shm", "-journal"):
            path = self.file("broker.sqlite3" + suffix)
            self.projects[path.stat().st_ino] = 0
            with self.assertRaises(BrokerError):
                self.registry._verify_sidecar(suffix)
            path.unlink()
            self.assertFalse(self.registry._verify_sidecar(suffix))
        seal = self.registry.seal_path
        self.projects[seal.stat().st_ino] = 0
        with self.assertRaises(BrokerError):
            self.registry._verify_seal()

    def test_legitimately_absent_sidecars_allow_repeated_durable_connections(self):
        self.charged_registry()
        for _ in range(2):
            for suffix in ("-wal", "-shm"):
                self.assertFalse(self.registry._verify_sidecar(suffix))
            self.registry.consume_request_nonce(os.urandom(16).hex(), 10)
            self.assertFalse(self.registry.dirty_path.exists())

    def test_new_marker_wrong_project_prevents_sqlite_open(self):
        self.charged_registry()
        self.new_project = 1
        with patch("synai.broker.registry.sqlite3.connect") as connect:
            with self.assertRaises(BrokerError):
                self.registry.consume_request_nonce("a" * 32, 10)
            connect.assert_not_called()
        self.assertTrue(self.registry.dirty_path.exists())

    def test_sidecar_mismatch_during_checkpoint_preserves_interruption(self):
        self.charged_registry()
        with self.assertRaises(BrokerError):
            with self.registry.connect():
                wal = self.registry.path.with_name("broker.sqlite3-wal")
                self.projects[wal.stat().st_ino] = 0
        self.assertTrue(self.registry.dirty_path.exists())
        self.assertIsNotNone(self.registry.failure_code)

    def test_interrupted_seal_creation_does_not_remove_marker(self):
        self.charged_registry()
        real_open = Directory.open
        def bad_new(directory, name, flags, mode=0):
            descriptor = real_open(directory, name, flags, mode)
            if name == "registry-identity.pending" and flags & os.O_CREAT:
                self.projects[os.fstat(descriptor).st_ino] = 0
            return descriptor
        with patch.object(Directory, "open", bad_new):
            with self.assertRaises(BrokerError):
                self.registry.consume_request_nonce("b" * 32, 10)
        self.assertTrue(self.registry.dirty_path.exists())
        self.assertTrue(self.registry.seal_path.with_suffix(".pending").exists())

    def test_database_quota_exhaustion_never_reseals_success(self):
        self.charged_registry()
        real_connect = sqlite3.connect
        class FullConnection(sqlite3.Connection):
            def execute(inner, sql, parameters=()):
                if sql.startswith("INSERT INTO broker_request_nonces"):
                    raise sqlite3.OperationalError("database or disk is full")
                return super().execute(sql, parameters)
        with patch("synai.broker.registry.sqlite3.connect",
                   side_effect=lambda *args, **kwargs: real_connect(*args, **kwargs, factory=FullConnection)):
            with self.assertRaises(BrokerError):
                self.registry.consume_request_nonce("c" * 32, 10)
        self.assertTrue(self.registry.dirty_path.exists())
        self.assertIsNotNone(self.registry.failure_code)

    def configure_logs(self):
        self.role = next(role for role in self.selected.storage_roles if role.kind == "broker_logs")
        self.revoked = Mock()
        self.log_handler = _configure_bounded_logging(
            self.selected, authority=self.authority(), on_failure=self.revoked,
        )
        return self.log_handler

    def test_log_creation_verifies_project_before_persistent_output(self):
        self.role = next(role for role in self.selected.storage_roles if role.kind == "broker_logs")
        self.new_project = 1
        with self.assertRaises(BrokerError):
            _configure_bounded_logging(self.selected, authority=self.authority())
        self.assertEqual((self.role.path / "broker.log").read_bytes(), b"")

    def test_existing_and_rotated_log_mismatch_prevents_logging(self):
        self.role = next(role for role in self.selected.storage_roles if role.kind == "broker_logs")
        for name in ("broker.log", "broker.log.1", "broker.log.pending"):
            with self.subTest(name=name):
                path = self.file(name)
                self.projects[path.stat().st_ino] = 0
                with self.assertRaises(BrokerError):
                    _configure_bounded_logging(self.selected, authority=self.authority())
                self.assertEqual(path.read_bytes(), b"historical")
                path.unlink()

    def test_external_log_rotation_is_refused_without_writing_new_file(self):
        handler = self.configure_logs()
        path = self.role.path / "broker.log"
        path.rename(self.role.path / "broker.log.1")
        self.file("broker.log", b"replacement")
        with self.assertRaises((BrokerError, RuntimeError)):
            handler.emit(logging.LogRecord("test", logging.INFO, "", 1, "must refuse", (), None))
        self.revoked.assert_called()
        self.assertEqual(path.read_bytes(), b"replacement")

    def test_log_quota_exhaustion_revokes_authority_without_fallback(self):
        handler = self.configure_logs()
        with patch("synai.broker.cli.os.write", side_effect=OSError(errno.EDQUOT, "quota exceeded")):
            with self.assertRaises(OSError):
                handler.emit(logging.LogRecord("test", logging.INFO, "", 1, "no fallback", (), None))
        self.revoked.assert_called_once()
        self.assertEqual((self.role.path / "broker.log").read_bytes(), b"")

    def test_log_emit_rechecks_file_project_and_shutdown_closes_authority(self):
        handler = self.configure_logs()
        handler.emit(logging.LogRecord("test", logging.INFO, "", 1, "bounded", (), None))
        path = self.role.path / "broker.log"
        before = path.read_bytes()
        self.projects[path.stat().st_ino] = 0
        with self.assertRaises(BrokerError):
            handler.emit(logging.LogRecord("test", logging.INFO, "", 1, "rejected", (), None))
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(BrokerError):
            handler.close()
        self.log_handler = None
        self.assertTrue(handler.stream.closed)
        self.assertEqual(handler.authority._chain, [])

    def test_logging_never_accepts_bare_path_authority(self):
        with self.assertRaises(RuntimeError):
            _configure_bounded_logging(self.selected)

    def test_exact_high5_capacity_totals_count_each_domain_once(self):
        totals = {
            kind: (sum(role.byte_limit for role in self.selected.storage_roles if role.kind == kind),
                   sum(role.inode_limit for role in self.selected.storage_roles if role.kind == kind))
            for kind in {role.kind for role in self.selected.storage_roles}
        }
        mib = 1024**2
        self.assertEqual(totals, {
            "task_slot": (1024 * mib, 16_384),
            "podman_graph": (4096 * mib, 131_072),
            "podman_runtime": (512 * mib, 16_384),
            "image_scratch": (512 * mib, 16_384),
            "broker_state": (512 * mib, 16_384),
            "broker_logs": (512 * mib, 8192),
            "temporary": (512 * mib, 16_384),
        })
        self.assertEqual(sum(value[0] for value in totals.values()), 7680 * mib)
        self.assertEqual(sum(value[1] for value in totals.values()), 221_184)
        required_bytes = 7680 * mib + self.selected.quota_reserve_bytes + 256 * mib
        required_inodes = 221_184 + self.selected.quota_reserve_inodes + 4096
        self.assertEqual(required_bytes, 16_911_433_728)
        self.assertEqual(required_inodes, 241_664)
        exact = {key: replace(value, filesystem_bytes_available=required_bytes,
                             filesystem_inodes_available=required_inodes)
                 for key, value in self.observations.items()}
        self.assertEqual(aggregate_storage_reasons(self.selected, exact), ())
        key = next(iter(exact))
        exact[key] = replace(exact[key], filesystem_bytes_available=required_bytes - 1)
        self.assertEqual(aggregate_storage_reasons(self.selected, exact), ("storage_aggregate_headroom",))

    def test_inspector_loss_and_changed_quota_never_establish_file_authority(self):
        path = self.file()
        with patch.object(self.manager.inspector, "inspect", side_effect=InspectorUnavailable("unavailable")):
            with self.authority() as directory:
                with self.assertRaises(BrokerError):
                    directory.open(path.name, os.O_WRONLY)
        self.observations[self.role.role_id] = replace(
            self.observations[self.role.role_id], byte_limit=self.role.byte_limit - 1,
        )
        with self.authority() as directory:
            with self.assertRaises(BrokerError):
                directory.open(path.name, os.O_WRONLY)
        self.assertEqual(path.read_bytes(), b"historical")

    def test_registry_failure_latches_readiness_despite_corrected_mock(self):
        service = BrokerService(self.selected)
        try:
            self.registry.failure_callback = service._registry_failure
            self.charged_registry()
            original = self.registry.path.read_bytes()
            inode = self.registry.path.stat().st_ino
            self.projects[inode] = 0
            with self.assertRaises(BrokerError):
                self.registry.consume_request_nonce("d" * 32, 10)
            self.assertFalse(service.readiness.execution_available)
            self.projects[inode] = self.role.project_id
            with self.assertRaises(BrokerError):
                self.registry.consume_request_nonce("e" * 32, 10)
            self.assertFalse(service.readiness.execution_available)
            self.assertEqual(self.registry.path.read_bytes(), original)
        finally:
            service.registry.close()

    def test_file_verification_immediately_revokes_authority_and_latches_failure(self):
        path = self.file()
        revoked = Mock()
        self.manager.persistent_file_failure_callback = revoked
        self.manager.verified = True
        self.manager.verified_slot_ids = frozenset({"slot-00"})
        self.projects[path.stat().st_ino] = 0
        with self.authority() as directory:
            with self.assertRaises(BrokerError):
                directory.open(path.name, os.O_WRONLY)
        revoked.assert_called_once_with("filesystem_file_quota_mismatch")
        self.assertFalse(self.manager.verified)
        self.assertFalse(self.manager.verified_slot_ids)
        self.projects[path.stat().st_ino] = self.role.project_id
        # Reading newly reverified evidence does not clear the assurance-loss latch.
        with self.authority() as directory:
            descriptor = directory.open(path.name, os.O_RDONLY)
            os.close(descriptor)
        self.assertEqual(self.manager.persistent_file_failure_code, "filesystem_file_quota_mismatch")
        self.assertFalse(self.manager.verified)

    def test_logging_quota_failure_does_not_restore_on_corrected_observation(self):
        handler = self.configure_logs()
        path = self.role.path / "broker.log"
        inode = path.stat().st_ino
        self.projects[inode] = 0
        with self.assertRaises(BrokerError):
            handler.emit(logging.LogRecord("test", logging.INFO, "", 1, "refused", (), None))
        self.projects[inode] = self.role.project_id
        # Logging itself is latched revoked, not merely Broker execution readiness.
        with self.assertRaises((BrokerError, RuntimeError)):
            handler.emit(logging.LogRecord("test", logging.INFO, "", 1, "still refused", (), None))
        self.assertEqual(path.read_bytes(), b"")
