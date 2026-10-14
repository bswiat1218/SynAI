from __future__ import annotations

import errno
import hashlib
import logging
import os
import socket
import stat
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from synai.broker.app import _persist_artifact_evidence
from synai.broker.cli import _PinnedLogHandler
from synai.broker.filesystem import Directory, mount_identity, open_beneath, read_file, remove_tree, write_new
from synai.broker.operations import execute_file_operation
from synai.broker.protocol import BrokerError, canonical_json, sign_claim
from synai.broker.quota_slots import QuotaSlotManager
from synai.broker.registry import BrokerRegistry, RegistryTrust
from synai.broker.runtime import RootlessPodmanRuntime
from synai.broker.config import BrokerReadiness
from synai.broker.snapshots import SnapshotProvenance, changed_files, stage_snapshot
from synai.broker.storage_inspector import InspectorInvalid, UnixStorageInspectorClient
from tests.test_broker import CLAIM_PRIVATE, config, claim


class DescriptorFilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "owned"
        self.root.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_openat2_is_used_and_never_falls_back(self) -> None:
        with Directory(self.root) as authority:
            write_new(authority, "file", b"inside")
            self.assertEqual(read_file(authority, "file", 10), b"inside")
            with patch("synai.broker.filesystem.platform.machine", return_value="unsupported"):
                with self.assertRaises(BrokerError) as denied:
                    authority.open("file", os.O_RDONLY)
            self.assertEqual(denied.exception.code, "openat2_unavailable")

    def test_kernel_symlink_escape_is_rejected_after_authority_acquisition(self) -> None:
        outside = Path(self.temporary.name) / "secret"
        outside.write_bytes(b"outside")
        with Directory(self.root) as authority:
            (self.root / "redirect").symlink_to(outside)
            with self.assertRaises(OSError) as denied:
                read_file(authority, "redirect", 1024)
            self.assertEqual(denied.exception.errno, errno.ELOOP)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_parent_replacement_revokes_authority_without_writing_replacement(self) -> None:
        with Directory(self.root) as authority:
            saved = self.root.with_name("saved")
            self.root.rename(saved)
            self.root.mkdir(mode=0o700)
            with self.assertRaises(BrokerError):
                write_new(authority, "evidence", b"must not write")
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertEqual(list(saved.iterdir()), [])

    def test_target_directory_rename_after_open_is_not_a_new_authority(self) -> None:
        with Directory(self.root) as root, root.mkdir("child") as child:
            (self.root / "child").rename(self.root / "old")
            (self.root / "child").mkdir(mode=0o700)
            with self.assertRaises(BrokerError):
                child.open("new", os.O_CREAT | os.O_WRONLY, 0o600)
            self.assertFalse((self.root / "child/new").exists())

    def test_mount_identity_change_is_refused(self) -> None:
        with Directory(self.root) as authority:
            original = mount_identity(authority.fd)
            with patch("synai.broker.filesystem.mount_identity", return_value=(*original[:2], original[2] + 1)):
                with self.assertRaises(BrokerError):
                    authority.revalidate()

    def test_untrusted_ancestors_and_changed_permissions_are_rejected(self) -> None:
        with Directory(self.root) as authority:
            os.chmod(self.root, 0o777)
            with self.assertRaises(BrokerError):
                authority.revalidate()
        with self.assertRaises(BrokerError):
            Directory(self.root)
        os.chmod(self.root, 0o700)

    def test_unexpected_descriptor_owner_is_rejected(self) -> None:
        with self.assertRaises(BrokerError):
            Directory(self.root, owner_uid=os.geteuid() + 1)

    def test_relative_path_traversal_and_mount_crossings_are_not_relaxed(self) -> None:
        with Directory(self.root) as root:
            with self.assertRaises(BrokerError):
                root.open("../escape", os.O_RDONLY)
            with patch("synai.broker.filesystem.open_beneath", side_effect=OSError(errno.EXDEV, "mount crossing")):
                with self.assertRaises(OSError):
                    root.open("file", os.O_RDONLY)

    def test_child_directory_symlink_is_rejected_by_artifact_collection(self) -> None:
        (self.root / "escape").symlink_to(Path(self.temporary.name))
        with self.assertRaises(BrokerError):
            changed_files(self.root, {"files": []})

    def test_artifact_directory_substitution_cannot_redirect_evidence(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir(mode=0o700)
        with Directory(self.root) as root:
            (self.root / "artifacts").symlink_to(outside)
            with self.assertRaises(BrokerError):
                _persist_artifact_evidence(root, "a" * 32, b"evidence")
        self.assertEqual(list(outside.iterdir()), [])

    def test_partial_snapshot_failure_preserves_evidence_and_never_cleans_foreign_content(self) -> None:
        content = b"x"
        manifest = {
            "schema_version": 1, "project_id": "2" * 32,
            "source_device_id": "8" * 32, "workspace_binding_id": "9" * 32,
            "snapshot_id": "3" * 32, "created_at": 1,
            "files": [{
                "path": "x", "file_type": "regular", "size_bytes": 1,
                "sha256": hashlib.sha256(content).hexdigest(),
            }],
            "total_bytes": 1,
            "source": {"kind": "paired_device", "key_fingerprint": "a" * 64, "protocol_version": 1},
        }
        digest = hashlib.sha256(canonical_json(manifest)).hexdigest()
        provenance = SnapshotProvenance(
            task_id="1" * 32, project_id=manifest["project_id"],
            snapshot_id=manifest["snapshot_id"], snapshot_digest=digest,
            lease_generation=1, import_operation_id="4" * 32,
        )
        with Directory(self.root) as root, patch(
            "synai.broker.snapshots._stage_file", side_effect=OSError("injected interruption"),
        ):
            with self.assertRaises(OSError):
                stage_snapshot(root, manifest, {"x": content}, provenance)
        self.assertFalse((self.root / "workspace").exists())
        self.assertTrue((self.root / f".importing-{provenance.import_operation_id}" / "provenance.json").exists())

    def test_cleanup_preflight_preserves_files_if_symlink_is_present(self) -> None:
        (self.root / "owned").write_bytes(b"evidence")
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"foreign")
        (self.root / "redirect").symlink_to(outside)
        with Directory(self.root) as root:
            with self.assertRaises(BrokerError):
                remove_tree(root)
        self.assertEqual((self.root / "owned").read_bytes(), b"evidence")
        self.assertEqual(outside.read_bytes(), b"foreign")

    def test_cleanup_parent_replacement_is_refused(self) -> None:
        with Directory(self.root) as root:
            self.root.rename(self.root.with_name("preserved"))
            self.root.mkdir(mode=0o700)
            (self.root / "foreign").write_bytes(b"foreign")
            with self.assertRaises(BrokerError):
                remove_tree(root)
        self.assertEqual((self.root / "foreign").read_bytes(), b"foreign")

    def test_atomic_write_rejects_parent_replacement_at_controlled_boundary(self) -> None:
        child = self.root / "child"
        child.mkdir(mode=0o700)
        (child / "file").write_bytes(b"before")
        original_fsync = os.fsync
        replaced = False

        def replace_parent(fd: int) -> None:
            nonlocal replaced
            original_fsync(fd)
            if not replaced and stat.S_ISREG(os.fstat(fd).st_mode):
                replaced = True
                child.rename(self.root / "old")
                child.mkdir(mode=0o700)
                (child / "file").write_bytes(b"foreign")

        with patch("synai.broker.operations.os.fsync", side_effect=replace_parent):
            with self.assertRaises(BrokerError):
                execute_file_operation(self.root, "write_file", {
                    "path": "child/file", "content": "after",
                    "expected_sha256": hashlib.sha256(b"before").hexdigest(),
                }, 1024)
        self.assertTrue(replaced)
        self.assertEqual((child / "file").read_bytes(), b"foreign")
        self.assertEqual((self.root / "old/file").read_bytes(), b"before")

    def test_descriptor_interruption_closes_authority(self) -> None:
        root = Directory(self.root)
        descriptor = root.fd
        with self.assertRaises(RuntimeError):
            with root:
                raise RuntimeError("interrupted")
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_pinned_log_replacement_revokes_logging_without_writing_foreign_file(self) -> None:
        root = Directory(self.root)
        descriptor = root.open("broker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stream = os.fdopen(descriptor, "a")
        revoked = Mock()
        handler = _PinnedLogHandler(stream, root, mount_identity(descriptor), revoked)
        try:
            (self.root / "broker.log").rename(self.root / "original")
            (self.root / "broker.log").write_text("foreign")
            with self.assertRaises(RuntimeError):
                handler.emit(logging.LogRecord("test", logging.ERROR, "", 0, "must not write", (), None))
            revoked.assert_called_once()
            self.assertEqual((self.root / "broker.log").read_text(), "foreign")
            self.assertEqual((self.root / "original").read_text(), "")
        finally:
            handler.close()

    def test_concurrent_creation_and_cleanup_reject_replaced_parent_with_barriers(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        with Directory(self.root) as root:
            def cleanup() -> None:
                entered.set()
                release.wait(timeout=2)
                try:
                    remove_tree(root)
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=cleanup)
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            self.root.rename(self.root.with_name("saved"))
            self.root.mkdir(mode=0o700)
            (self.root / "foreign").write_bytes(b"preserve")
            release.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BrokerError)
        self.assertEqual((self.root / "foreign").read_bytes(), b"preserve")


class StatePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.selected = config(Path(self.temporary.name) / "broker")
        self.registry = BrokerRegistry(self.selected)
        self.registry.initialize(storage_verified=True, provision=True)

    def tearDown(self) -> None:
        self.registry.close()
        self.temporary.cleanup()

    def test_registry_parent_replacement_during_connection_does_not_initialize_new_state(self) -> None:
        replacement = self.selected.state_dir
        saved = replacement.with_name("saved-state")
        with self.assertRaises(BrokerError):
            with self.registry.connect() as connection:
                replacement.rename(saved)
                replacement.mkdir(mode=0o700)
                (replacement / "foreign").write_bytes(b"foreign")
                connection.execute("INSERT INTO broker_request_nonces VALUES (?, ?)", ("a" * 32, 100))
        self.assertEqual(self.registry.trust, RegistryTrust.MISMATCH)
        self.assertFalse((replacement / "broker.sqlite3").exists())
        self.assertEqual((replacement / "foreign").read_bytes(), b"foreign")
        self.assertTrue((saved / "registry-interrupted").exists())

    def test_database_replacement_cannot_be_resealed_as_authoritative(self) -> None:
        old = self.registry.path.with_suffix(".saved")
        with self.assertRaises(BrokerError):
            with self.registry.connect():
                self.registry.path.rename(old)
                self.registry.path.write_bytes(b"foreign")
                os.chmod(self.registry.path, 0o600)
        self.assertTrue(self.registry.dirty_path.exists())
        self.assertEqual(self.registry.path.read_bytes(), b"foreign")
        self.assertEqual(self.registry.trust, RegistryTrust.MISMATCH)

    def test_seal_symlink_is_refused_without_reading_foreign_evidence(self) -> None:
        self.registry.seal_path.unlink()
        self.registry.seal_path.symlink_to("/etc/passwd")
        with self.assertRaises(BrokerError):
            self.registry.container_records()
        self.assertIsNotNone(self.registry.failure_code)

    def test_slot_creation_rechecks_assignment_and_rejects_existing_task(self) -> None:
        request = claim("prepare", {})
        self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        slot = self.selected.quota_slots[0]
        manager = QuotaSlotManager(self.selected, self.registry)
        manager.verified = True
        manager.verified_slot_ids = frozenset({slot.slot_id})
        root = slot.path / request.task_id
        root.mkdir(mode=0o700)
        (root / "evidence").write_bytes(b"preserve")
        with patch.object(manager, "open_storage_role", side_effect=lambda _: Directory(slot.path)):
            with self.assertRaises(BrokerError):
                manager.create_task_root(request.task_id)
        self.assertEqual((root / "evidence").read_bytes(), b"preserve")
        self.assertTrue(self.registry.has_quarantined_slots())

    def test_wrong_filesystem_never_establishes_role_authority(self) -> None:
        manager = QuotaSlotManager(self.selected, self.registry)
        manager.inspector = Mock()
        manager._verify_observation = Mock()
        with patch("synai.broker.quota_slots._mount_for_fd", side_effect=ValueError("not ext4")):
            with self.assertRaises(ValueError):
                manager.open_storage_role(self.selected.storage_roles[0].role_id)

    def test_stale_slot_assignment_is_rejected_before_creation(self) -> None:
        request = claim("prepare", {})
        self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        manager = QuotaSlotManager(self.selected, self.registry)
        manager.verified = True
        manager.verified_slot_ids = frozenset({"slot-00"})
        original = dict(self.registry.task_slot(request.task_id))
        changed = {**original, "allocation_id": "f" * 32}
        with patch.object(self.registry, "task_slot", side_effect=[original, changed]), patch.object(
            manager, "open_storage_role", side_effect=lambda _: Directory(self.selected.quota_slots[0].path),
        ):
            with self.assertRaises(BrokerError):
                manager.create_task_root(request.task_id)
        self.assertFalse((self.selected.quota_slots[0].path / request.task_id).exists())


class InspectorEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "inspector.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.client = UnixStorageInspectorClient(
            self.path, (), inspector_uid=os.geteuid(), inspector_gid=os.getegid(),
            timeout_seconds=0.1, max_age_seconds=5, inventory_generation="a" * 64,
        )

    def tearDown(self) -> None:
        self.listener.close()
        self.temporary.cleanup()

    def test_actual_connected_peer_credentials_are_required(self) -> None:
        connection = Mock()
        connection.getsockopt.return_value = struct.pack("3i", 123, os.geteuid() + 1, os.getegid())
        with self.assertRaises(InspectorInvalid):
            self.client._verify_peer(connection)

    def test_socket_replacement_after_open_is_rejected_before_request_transmission(self) -> None:
        fake = Mock()
        fake.__enter__ = Mock(return_value=fake)
        fake.__exit__ = Mock(return_value=False)
        fake.getsockopt.return_value = struct.pack("3i", 123, os.geteuid(), os.getegid())
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        def replace_socket(_: str) -> None:
            self.path.unlink()
            replacement.bind(str(self.path))
            os.chmod(self.path, 0o600)

        fake.connect.side_effect = replace_socket
        try:
            with patch("synai.broker.storage_inspector.socket.socket", return_value=fake):
                with self.assertRaises(InspectorInvalid):
                    self.client._exchange({"request": "bounded"})
            fake.sendall.assert_not_called()
        finally:
            replacement.close()

    def test_same_process_socket_descriptor_resolves_original_listener(self) -> None:
        self.listener.listen(1)
        with Directory(self.path.parent) as directory:
            descriptor = directory.open(self.path.name, os.O_PATH)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(0.2)
                    connection.connect(f"/proc/self/fd/{descriptor}")
                    self.client._verify_peer(connection)
                    accepted, _ = self.listener.accept()
                    accepted.close()
            finally:
                os.close(descriptor)


class BindSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unaccepted_external_bind_resolution_never_invokes_podman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = config(Path(temporary) / "broker")
            registry = BrokerRegistry(selected)
            registry.initialize(storage_verified=True, provision=True)
            request = claim("prepare", {})
            registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
            registry.transition(request.task_id, "ready")
            registry.reserve_container(request.task_id, request.allocation_id)
            workspace = selected.quota_slots[0].path / request.task_id / "workspace"
            workspace.parent.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            runtime = RootlessPodmanRuntime(selected, registry)
            runtime.readiness = BrokerReadiness(True, True, {}, ())
            runtime.workspace_authority = lambda _: Directory(workspace)
            runtime._run = AsyncMock()
            with self.assertRaises(BrokerError) as denied:
                await runtime.create_task(request.task_id, request.allocation_id, workspace)
            self.assertEqual(denied.exception.code, "bind_source_resolution_unaccepted")
            runtime._run.assert_not_awaited()
            with Directory(workspace) as pinned:
                workspace.rename(workspace.with_name("saved"))
                workspace.mkdir(mode=0o700)
                runtime.workspace_authority = lambda _: pinned
                with self.assertRaises(BrokerError):
                    await runtime.create_task(request.task_id, request.allocation_id, workspace)
            runtime._run.assert_not_awaited()
            registry.close()
