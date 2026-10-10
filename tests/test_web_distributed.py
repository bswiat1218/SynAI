from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from synai.web.app import create_app
from synai.web.config import WebConfig
from synai.web.database import MetadataDatabase
from synai.web.distributed import (
    DEVICE_CREDENTIAL_LIFETIME_SECONDS,
    DevicePrincipal,
    DistributedError,
    DistributedRegistry,
    Enrollment,
    SUPPORTED_PROTOCOL_VERSIONS,
    canonical_json,
    device_request_message,
    enrollment_message,
)
from synai.web.snapshots import SnapshotLimits, SnapshotStore


PASSWORD = "correct horse battery staple"
ORIGIN = "http://127.0.0.1:8765"
CAPABILITIES = {
    "agent_version": "test-client/1",
    "platform": "linux",
    "architecture": "x86_64",
    "features": ["snapshot-v1"],
}


class _Provider:
    async def list_models(self):
        return []

    async def capabilities(self, name: str):
        raise NotImplementedError

    async def chat(self, model: str, messages: list, tools: list):
        raise AssertionError("Distributed Phase 13C must not execute AI tasks")
        yield


class DistributedFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = MetadataDatabase(self.root / "data")
        self.database.initialize()
        self.registry = DistributedRegistry(self.database)
        self.snapshots = SnapshotStore(self.database, self.registry)
        self.snapshots.initialize()
        self.key = Ed25519PrivateKey.generate()
        self.challenge = self.registry.create_pairing_challenge(now=1000)
        public_key = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        self.public_key = public_key
        signed = enrollment_message(
            self.challenge.challenge_id, self.challenge.secret,
            public_key, 1, CAPABILITIES,
        )
        self.enrollment = Enrollment(
            self.challenge.challenge_id,
            self.challenge.secret,
            base64.b64encode(public_key).decode("ascii"),
            1,
            CAPABILITIES,
            base64.b64encode(self.key.sign(signed)).decode("ascii"),
        )
        self.pending_credential = self.registry.enroll(self.enrollment, now=1001)
        self.device_id = self.pending_credential.device_id
        self.project = self.registry.create_project("A logical project", "project-key-000001", now=1002)
        self.registry.authorize_device(self.device_id, now=1003)
        self.credential = self.pending_credential.credential
        self.binding = self.registry.create_binding(
            str(self.project.project_id), self.device_id, "My laptop", now=1004,
        )
        self.principal = self.authenticate(now=1005)

    def authenticate(self, now: int = 1005, nonce: str | None = None) -> DevicePrincipal:
        raw_nonce = nonce or secrets.token_urlsafe(18)
        timestamp = str(now)
        body = b""
        signed = device_request_message("GET", "/device/test", now, raw_nonce, body)
        return self.registry.authenticate_device(
            self.credential, self.device_id, timestamp, raw_nonce,
            base64.b64encode(self.key.sign(signed)).decode("ascii"),
            "GET", "/device/test", body, now=now,
        )

    def test_pairing_requires_proof_operator_authorization_and_single_use(self) -> None:
        self.assertEqual(self.pending_credential.state, "pending")
        self.assertEqual(self.registry.device_metadata(self.device_id, now=1002)["state"], "authorized")
        challenge = self.registry.create_pairing_challenge(now=2000)
        key = Ed25519PrivateKey.generate()
        public_key = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        proof = key.sign(enrollment_message(
            challenge.challenge_id, challenge.secret, public_key, 1, CAPABILITIES,
        ))
        pending = self.registry.enroll(Enrollment(
            challenge.challenge_id, challenge.secret,
            base64.b64encode(public_key).decode("ascii"), 1, CAPABILITIES,
            base64.b64encode(proof).decode("ascii"),
        ), now=2001)
        self.assertEqual(pending.state, "pending")
        with self.assertRaises(DistributedError) as unauthorized:
            self.registry.create_binding(str(self.project.project_id), pending.device_id, "nope", now=2002)
        self.assertEqual(unauthorized.exception.code, "device_not_authorized")
        with self.assertRaises(DistributedError) as replay:
            self.registry.enroll(self.enrollment, now=1002)
        self.assertEqual(replay.exception.code, "pairing_replayed_or_expired")

    def test_forged_proof_expired_pairing_and_protocol_rejected(self) -> None:
        challenge = self.registry.create_pairing_challenge(now=10)
        forged = Enrollment(
            challenge.challenge_id,
            challenge.secret,
            self.enrollment.public_key,
            1,
            CAPABILITIES,
            self.enrollment.signature,
        )
        with self.assertRaises(DistributedError) as bad_proof:
            self.registry.enroll(forged, now=11)
        self.assertEqual(bad_proof.exception.code, "device_proof_invalid")
        with self.assertRaises(DistributedError) as expired:
            self.registry.enroll(self.enrollment, now=1301)
        self.assertEqual(expired.exception.code, "pairing_replayed_or_expired")

        unsupported = Enrollment(
            challenge.challenge_id, challenge.secret, self.enrollment.public_key,
            99, CAPABILITIES, self.enrollment.signature,
        )
        with self.assertRaises(DistributedError) as version:
            self.registry.enroll(unsupported, now=11)
        self.assertEqual(version.exception.code, "protocol_unsupported")
        self.assertEqual(SUPPORTED_PROTOCOL_VERSIONS, frozenset({1}))

    def test_concurrent_enrollment_consumes_challenge_once(self) -> None:
        challenge = self.registry.create_pairing_challenge()
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        signature = key.sign(enrollment_message(
            challenge.challenge_id, challenge.secret, public, 1, CAPABILITIES,
        ))
        request = Enrollment(
            challenge.challenge_id, challenge.secret,
            base64.b64encode(public).decode("ascii"), 1, CAPABILITIES,
            base64.b64encode(signature).decode("ascii"),
        )

        def enroll():
            try:
                return self.registry.enroll(request)
            except DistributedError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: enroll(), range(2)))
        self.assertEqual(sum(not isinstance(value, DistributedError) for value in outcomes), 1)
        self.assertEqual(sum(isinstance(value, DistributedError) for value in outcomes), 1)

    def test_device_auth_replay_expiration_revocation_and_credential_rotation(self) -> None:
        metadata = self.registry.device_metadata(self.device_id, now=1006)
        self.assertEqual(metadata["state"], "authorized")
        self.assertEqual(metadata["last_authenticated_activity_at"], 1005)
        self.assertTrue(metadata["recently_active"])
        self.assertEqual(metadata["connection_state"], "not_supported")
        self.assertFalse(metadata["connected"])

        with self.assertRaises(DistributedError) as replay:
            timestamp, nonce = 1005, "replay-nonce-abcdefghijkl"
            signature = base64.b64encode(self.key.sign(device_request_message(
                "GET", "/device/test", timestamp, nonce, b"",
            ))).decode("ascii")
            headers = (
                self.credential, self.device_id, str(timestamp), nonce, signature,
                "GET", "/device/test", b"",
            )
            self.registry.authenticate_device(*headers, now=timestamp)
            self.registry.authenticate_device(*headers, now=timestamp)
        self.assertEqual(replay.exception.code, "device_request_replayed")
        with self.assertRaises(DistributedError) as stale:
            timestamp, nonce = 1005, secrets.token_urlsafe(18)
            proof = base64.b64encode(self.key.sign(device_request_message(
                "GET", "/device/test", timestamp, nonce, b"",
            ))).decode("ascii")
            self.registry.authenticate_device(
                self.credential, self.device_id, str(timestamp), nonce, proof,
                "GET", "/device/test", b"", now=1100,
            )
        self.assertEqual(stale.exception.code, "device_request_expired")
        old_principal = self.principal
        rotated = self.registry.rotate_device_credential(old_principal, now=1006)
        self.assertEqual(rotated.credential_expires_at, 1006 + DEVICE_CREDENTIAL_LIFETIME_SECONDS)
        with self.assertRaises(DistributedError):
            self.registry.authenticate_device(
                self.credential, self.device_id, "1007", secrets.token_urlsafe(18),
                "", "GET", "/device/test", b"", now=1007,
            )
        self.registry.revoke_device(self.device_id, now=1008)
        with self.assertRaises(DistributedError) as revoked:
            self.registry.authenticate_device(
                rotated.credential, self.device_id, "1009", secrets.token_urlsafe(18),
                "", "GET", "/device/test", b"", now=1009,
            )
        self.assertEqual(revoked.exception.code, "device_credential_invalid")

    def test_logical_identity_dedup_binding_ownership_and_staleness(self) -> None:
        duplicate = self.registry.create_project("A logical project", "project-key-000001", now=2000)
        self.assertEqual(duplicate.project_id, self.project.project_id)
        with self.assertRaises(DistributedError) as conflict:
            self.registry.create_project("Different name", "project-key-000001", now=2000)
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

        other_project = self.registry.create_project("Other project", "project-key-000002")
        with self.assertRaises(DistributedError) as cross_project:
            self.registry.require_binding(
                str(other_project.project_id), self.device_id,
                str(self.binding.binding_id), now=1005,
            )
        self.assertEqual(cross_project.exception.code, "binding_not_found")
        expired = self.registry.create_binding(
            str(other_project.project_id), self.device_id, "Temporary", expires_at=1100, now=1000,
        )
        self.assertEqual(self.registry.list_bindings(str(other_project.project_id), now=1100)[0].status, "stale")
        with self.assertRaises(DistributedError) as stale:
            self.registry.require_binding(
                str(other_project.project_id), self.device_id, str(expired.binding_id), now=1100,
            )
        self.assertEqual(stale.exception.code, "binding_stale")

    def test_snapshot_upload_integrity_privacy_and_index_adapter(self) -> None:
        now = int(time.time())
        execution_marker = self.root / "source-was-executed"
        source = (
            "def useful_symbol():\n"
            "    return 'evidence is immutable'\n"
            f"open({str(execution_marker)!r}, 'w').write('bad')\n"
        ).encode()
        manifest = [{
            "path": "src/example.py",
            "file_type": "regular",
            "size_bytes": len(source),
            "sha256": hashlib.sha256(source).hexdigest(),
        }]
        upload = self.snapshots.begin(
            self.principal, str(self.project.project_id),
            str(self.binding.binding_id), manifest,
            idempotency_key="snapshot-upload-key-0001", now=now,
        )
        upload_id = str(upload["upload_id"])
        duplicate_upload = self.snapshots.begin(
            self.principal, str(self.project.project_id),
            str(self.binding.binding_id), manifest,
            idempotency_key="snapshot-upload-key-0001", now=now,
        )
        self.assertEqual(duplicate_upload["upload_id"], upload_id)
        self.assertTrue(duplicate_upload["reused"])
        chunk_size = self.snapshots.limits.max_chunk_bytes
        for index, offset in enumerate(range(0, len(source), chunk_size)):
            block = source[offset:offset + chunk_size]
            accepted = self.snapshots.accept_chunk(
                self.principal, upload_id, 0, index, block, now=now + 1,
            )
            self.assertFalse(accepted["duplicate"])
            self.assertTrue(self.snapshots.accept_chunk(
                self.principal, upload_id, 0, index, block, now=now + 1,
            )["duplicate"])
        committed = self.snapshots.commit(self.principal, upload_id, now=now + 2)
        self.assertEqual(committed["state"], "available")
        snapshot_id = str(committed["snapshot_id"])
        manifest_digest = hashlib.sha256(canonical_json(committed["manifest"])).hexdigest()
        self.assertEqual(manifest_digest, committed["manifest_digest"])
        source_adapter = self.snapshots.open_source(str(self.project.project_id), snapshot_id, now=now + 3)
        self.assertEqual(source_adapter.verify_indexed_sources(), {"verified_files": 1, "manifest_files": 1})
        query = source_adapter.query("find_symbol", {"name": "useful_symbol"})
        self.assertTrue(query["results"])
        context = source_adapter.build_context("Explain useful_symbol")
        self.assertTrue(any(item.path == "src/example.py" for item in context.items))
        source_path = source_adapter.root / "src" / "example.py"
        self.assertEqual(source_path.read_bytes(), source)
        self.assertFalse(source_path.stat().st_mode & 0o222)
        self.assertFalse(execution_marker.exists())
        snapshot_directory = self.snapshots.snapshots / snapshot_id
        self.assertFalse(snapshot_directory.stat().st_mode & 0o222)
        self.assertEqual(
            self.snapshots.snapshot_status(str(self.project.project_id), snapshot_id)["snapshot_id"],
            snapshot_id,
        )
        with self.assertRaises(DistributedError) as cross:
            self.snapshots.snapshot_status("f" * 32, snapshot_id)
        self.assertEqual(cross.exception.code, "snapshot_not_found")
        self.assertEqual(source_adapter.index.read_file_bytes("src/example.py"), source)

    def test_manifest_rejects_traversal_collisions_unsupported_and_oversize(self) -> None:
        def manifest(path: str, size: int = 0, file_type: str = "regular"):
            return [{
                "path": path,
                "file_type": file_type,
                "size_bytes": size,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }]

        for path in (
            "../escape.py", "/absolute.py", "a\\b.py", "a//b.py",
            "C:/absolute.py", "src/CON.py", "src/invalid:name.py",
        ):
            with self.subTest(path=path), self.assertRaises(DistributedError):
                self.snapshots.begin(
                    self.principal, str(self.project.project_id),
                    str(self.binding.binding_id), manifest(path), now=1010,
                )
        with self.assertRaises(DistributedError) as collision:
            self.snapshots.begin(
                self.principal, str(self.project.project_id), str(self.binding.binding_id),
                manifest("Src/File.py") + manifest("src/file.py"), now=1010,
            )
        self.assertEqual(collision.exception.code, "snapshot_path_collision")
        with self.assertRaises(DistributedError) as unicode_collision:
            self.snapshots.begin(
                self.principal, str(self.project.project_id), str(self.binding.binding_id),
                manifest("café.py") + manifest("cafe\u0301.py"), now=1010,
            )
        self.assertEqual(unicode_collision.exception.code, "snapshot_path_collision")
        for unsupported in ("archive.zip", "image.png"):
            with self.subTest(path=unsupported), self.assertRaises(DistributedError):
                self.snapshots.begin(
                    self.principal, str(self.project.project_id),
                    str(self.binding.binding_id), manifest(unsupported), now=1010,
                )
        for excluded in (".env", "src/.env.production", ".git/config", "keys/id_rsa", "private.pem"):
            with self.subTest(path=excluded), self.assertRaises(DistributedError):
                self.snapshots.begin(
                    self.principal, str(self.project.project_id),
                    str(self.binding.binding_id), manifest(excluded), now=1010,
                )
        with self.assertRaises(DistributedError) as link:
            self.snapshots.begin(
                self.principal, str(self.project.project_id), str(self.binding.binding_id),
                manifest("link.py", file_type="symlink"), now=1010,
            )
        self.assertEqual(link.exception.code, "snapshot_file_type_unsupported")
        with self.assertRaises(DistributedError):
            self.snapshots.begin(
                self.principal, str(self.project.project_id), str(self.binding.binding_id),
                manifest("too-big.py", self.snapshots.limits.max_file_bytes + 1), now=1010,
            )

    def test_snapshot_tampering_incomplete_expiry_and_restart(self) -> None:
        now = int(time.time())
        content = b"def x():\n    return 1\n"
        entry = [{
            "path": "x.py", "file_type": "regular", "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }]
        upload = self.snapshots.begin(
            self.principal, str(self.project.project_id), str(self.binding.binding_id),
            entry, now=now,
        )
        upload_id = str(upload["upload_id"])
        with self.assertRaises(DistributedError) as incomplete:
            self.snapshots.commit(self.principal, upload_id, now=now + 1)
        self.assertEqual(incomplete.exception.code, "snapshot_integrity_error")
        self.snapshots.accept_chunk(self.principal, upload_id, 0, 0, content, now=now + 1)
        snapshot = self.snapshots.commit(self.principal, upload_id, now=now + 2)
        snapshot_id = str(snapshot["snapshot_id"])
        persisted_store = SnapshotStore(self.database, self.registry)
        persisted_store.initialize()
        self.assertEqual(
            persisted_store.snapshot_status(str(self.project.project_id), snapshot_id)["manifest_digest"],
            snapshot["manifest_digest"],
        )
        object_path = persisted_store.snapshots / snapshot_id / "objects" / "0000"
        os.chmod(object_path, 0o600)
        object_path.write_bytes(b"tampered")
        with self.assertRaises(DistributedError) as tampered:
            persisted_store.open_source(str(self.project.project_id), snapshot_id, now=now + 3)
        self.assertEqual(tampered.exception.code, "snapshot_integrity_error")
        with self.assertRaises(DistributedError) as expired:
            persisted_store.snapshot_status(str(self.project.project_id), snapshot_id, now=now + 31 * 86400)
        self.assertEqual(expired.exception.code, "snapshot_expired")

    def test_source_adapter_rejects_symlinked_source_view(self) -> None:
        now = int(time.time())
        data = b"def safe():\n    return True\n"
        entry = [{
            "path": "src/safe.py", "file_type": "regular", "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }]
        upload = self.snapshots.begin(
            self.principal, str(self.project.project_id), str(self.binding.binding_id),
            entry, now=now,
        )
        self.snapshots.accept_chunk(
            self.principal, upload["upload_id"], 0, 0, data, now=now + 1,
        )
        snapshot = self.snapshots.commit(self.principal, upload["upload_id"], now=now + 2)
        adapter = self.snapshots.open_source(
            str(self.project.project_id), str(snapshot["snapshot_id"]), now=now + 3,
        )
        source_directory = adapter.root / "src"
        adapter.root.chmod(0o700)
        source_directory.chmod(0o700)
        source_directory.rename(adapter.root / "src-original")
        source_directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(DistributedError) as unsafe:
            self.snapshots.open_source(
                str(self.project.project_id), str(snapshot["snapshot_id"]), now=now + 4,
            )
        self.assertEqual(unsafe.exception.code, "snapshot_integrity_error")

    def test_interrupted_upload_restart_concurrent_commit_and_referenced_retention(self) -> None:
        now = int(time.time())
        limits = SnapshotLimits(
            max_files=4,
            max_file_bytes=64,
            max_total_bytes=128,
            max_path_depth=4,
            max_path_length=128,
            max_chunk_bytes=4,
            max_concurrent_uploads_per_device=1,
            max_temporary_bytes=1024,
            max_storage_bytes=1024,
            upload_deadline_seconds=30,
            retention_seconds=300,
        )
        store = SnapshotStore(self.database, self.registry, limits)
        store.initialize()
        content = b"ten-bytes"
        entry = [{
            "path": "readme.txt", "file_type": "regular", "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }]
        upload = store.begin(
            self.principal, str(self.project.project_id), str(self.binding.binding_id),
            entry, idempotency_key="interrupted-upload-key-01", now=now,
        )
        upload_id = str(upload["upload_id"])
        store.accept_chunk(self.principal, upload_id, 0, 0, content[:4], now=now + 1)
        restarted = SnapshotStore(self.database, self.registry, limits)
        restarted.initialize()
        restarted.accept_chunk(self.principal, upload_id, 0, 1, content[4:8], now=now + 2)
        restarted.accept_chunk(self.principal, upload_id, 0, 2, content[8:], now=now + 3)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda _: restarted.commit(self.principal, upload_id, now=now + 4),
                range(2),
            ))
        self.assertEqual(results[0]["snapshot_id"], results[1]["snapshot_id"])
        snapshot_id = results[0]["snapshot_id"]
        contract = self.registry.create_disabled_task_contract(
            str(self.project.project_id), snapshot_id, ("repository-index",), now=now + 5,
        )
        self.assertEqual(contract.state, "not_enabled")
        self.assertEqual(contract.lease_generation, 0)
        self.assertIsNone(contract.selected_execution_target)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE immutable_snapshots SET expires_at = ? WHERE snapshot_id = ?",
                (now + 10, snapshot_id),
            )
            connection.execute(
                "UPDATE distributed_tasks SET state = 'claimed', execution_target = ?, "
                "execution_claim = ?, fencing_generation = 4 WHERE task_id = ?",
                ("test-target", "test-claim", str(contract.task_id)),
            )
        restarted.cleanup(now + 11)
        self.assertEqual(
            restarted.snapshot_status(str(self.project.project_id), snapshot_id, now=now + 11)["state"],
            "available",
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE distributed_tasks SET state = 'completed' WHERE task_id = ?",
                (str(contract.task_id),),
            )
        restarted.cleanup(now + 12)
        with self.assertRaises(DistributedError) as expired:
            restarted.snapshot_status(str(self.project.project_id), snapshot_id, now=now + 12)
        self.assertEqual(expired.exception.code, "snapshot_expired")

        abandoned = restarted.begin(
            self.principal, str(self.project.project_id), str(self.binding.binding_id),
            [{
                "path": "unfinished.txt", "file_type": "regular", "size_bytes": 9,
                "sha256": hashlib.sha256(b"abandoned").hexdigest(),
            }],
            idempotency_key="abandoned-upload-key-0001", now=now + 20,
        )
        unfinished_path = restarted.uploads / abandoned["upload_id"]
        self.assertTrue(unfinished_path.exists())
        restarted.cleanup(now + 51)
        self.assertFalse(unfinished_path.exists())
        with self.assertRaises(DistributedError) as upload_expired:
            restarted.upload_status(self.principal, abandoned["upload_id"], now=now + 51)
        self.assertEqual(upload_expired.exception.code, "upload_expired")

    def test_snapshot_listing_isolates_expired_records_and_retains_task_sources(self) -> None:
        now = int(time.time())
        snapshot_ids: list[str] = []
        for index, content in enumerate((b"expired", b"retained", b"available")):
            manifest = [{
                "path": f"source-{index}.txt",
                "file_type": "regular",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }]
            upload = self.snapshots.begin(
                self.principal,
                str(self.project.project_id),
                str(self.binding.binding_id),
                manifest,
                idempotency_key=f"mixed-snapshot-listing-{index:02d}",
                now=now,
            )
            self.snapshots.accept_chunk(
                self.principal, str(upload["upload_id"]), 0, 0, content, now=now + 1,
            )
            snapshot = self.snapshots.commit(
                self.principal, str(upload["upload_id"]), now=now + 2,
            )
            snapshot_ids.append(str(snapshot["snapshot_id"]))

        retained_task = self.registry.create_disabled_task_contract(
            str(self.project.project_id), snapshot_ids[1], (), now=now + 2,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE immutable_snapshots SET expires_at = ? WHERE snapshot_id IN (?, ?)",
                (now + 1, snapshot_ids[0], snapshot_ids[1]),
            )
            connection.execute(
                "UPDATE distributed_tasks SET state = 'queued' WHERE task_id = ?",
                (str(retained_task.task_id),),
            )

        listed = self.snapshots.list_snapshots(str(self.project.project_id), now=now + 4)
        listed_ids = [str(snapshot["snapshot_id"]) for snapshot in listed]
        self.assertEqual(listed_ids, sorted([snapshot_ids[1], snapshot_ids[2]], reverse=True))
        with self.assertRaises(DistributedError) as expired:
            self.snapshots.snapshot_status(
                str(self.project.project_id), snapshot_ids[0], now=now + 4,
            )
        self.assertEqual(expired.exception.code, "snapshot_expired")

    def test_memory_preview_is_read_only_and_does_not_touch_legacy_projects(self) -> None:
        with self.database.connect() as connection:
            before = connection.execute("SELECT count(*) FROM memory_associations").fetchone()[0]
            legacy_count = connection.execute("SELECT count(*) FROM projects").fetchone()[0]
        preview = self.registry.authorize_memory_association_preview(
            hashlib.sha256(b"legacy").hexdigest(),
            str(self.project.project_id),
            {"source": "operator_preview", "source_version": 1},
            now=1010,
        )
        self.assertFalse(preview["migration_enabled"])
        self.assertIsNone(preview["record_count"])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM memory_associations").fetchone()[0], before)
            self.assertEqual(connection.execute("SELECT count(*) FROM projects").fetchone()[0], legacy_count)

    def test_schema_three_preserves_existing_v2_web_records(self) -> None:
        legacy_database = MetadataDatabase(self.root / "legacy-data")
        path = legacy_database.path
        legacy_database.storage.initialize()
        legacy_database._private_directory(legacy_database.directory)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE credentials(singleton INTEGER PRIMARY KEY, password_hash TEXT NOT NULL);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
            CREATE TABLE login_limits(peer_hash TEXT PRIMARY KEY, window_start INTEGER NOT NULL, attempts INTEGER NOT NULL, blocked_until INTEGER NOT NULL);
            CREATE TABLE projects(project_id TEXT PRIMARY KEY, workspace_key TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, device_id INTEGER NOT NULL, inode INTEGER NOT NULL, owner_uid INTEGER NOT NULL, registered_at INTEGER NOT NULL);
            CREATE TABLE workspace_lease_generations(workspace_key TEXT NOT NULL, generation INTEGER NOT NULL, owner_id TEXT NOT NULL, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL, label TEXT NOT NULL, token_hash TEXT NOT NULL, acquired_at INTEGER NOT NULL, released_at INTEGER, status TEXT NOT NULL, PRIMARY KEY(workspace_key, generation));
            INSERT INTO projects VALUES ('legacy-project', 'old', 'Old', 1, 2, 3, 4);
            INSERT INTO sessions VALUES ('session-hash', 'csrf-hash', 5, 6);
            PRAGMA user_version = 2;
            """
        )
        connection.close()
        os.chmod(path, 0o600)
        legacy_database.initialize()
        with legacy_database.connect() as migrated:
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(migrated.execute("SELECT project_id FROM projects").fetchone()[0], "legacy-project")
            self.assertEqual(migrated.execute("SELECT token_hash FROM sessions").fetchone()[0], "session-hash")
            self.assertEqual(migrated.execute("SELECT count(*) FROM logical_projects").fetchone()[0], 0)


class DistributedApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(
            WebConfig(Path(self.temp.name) / "data", (), initial_password=PASSWORD),
            provider=_Provider(),
        )
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        login = self.client.post(
            "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN},
        )
        self.assertEqual(login.status_code, 200)
        self.csrf = login.json()["csrf_token"]
        self.operator_headers = {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}

    def _enroll(self) -> tuple[str, str, Ed25519PrivateKey]:
        challenge = self.client.post(
            "/api/v1/devices/pairing-challenges", headers=self.operator_headers,
        )
        self.assertEqual(challenge.status_code, 201, challenge.text)
        data = challenge.json()
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        signature = key.sign(enrollment_message(
            data["challenge_id"], data["challenge_secret"], public, 1, CAPABILITIES,
        ))
        response = self.client.post(
            "/api/v1/device-enrollments",
            json={
                "challenge_id": data["challenge_id"],
                "challenge_secret": data["challenge_secret"],
                "public_key": base64.b64encode(public).decode("ascii"),
                "protocol_version": 1,
                "capabilities": CAPABILITIES,
                "signature": base64.b64encode(signature).decode("ascii"),
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        enrollment = response.json()
        authorized = self.client.post(
            f"/api/v1/devices/{enrollment['device_id']}/authorize",
            headers=self.operator_headers,
        )
        self.assertEqual(authorized.status_code, 200, authorized.text)
        return enrollment["device_id"], enrollment["credential"], key

    @staticmethod
    def _device_headers(
        device_id: str,
        credential: str,
        key: Ed25519PrivateKey,
        method: str,
        path: str,
        body: bytes,
    ) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(18)
        signature = key.sign(device_request_message(method, path, timestamp, nonce, body))
        return {
            "X-SynAI-Device-ID": device_id,
            "X-SynAI-Device-Credential": credential,
            "X-SynAI-Device-Timestamp": str(timestamp),
            "X-SynAI-Device-Nonce": nonce,
            "X-SynAI-Device-Signature": base64.b64encode(signature).decode("ascii"),
        }

    def test_authenticated_logical_project_device_binding_snapshot_flow(self) -> None:
        project_body = canonical_json({"name": "API logical project", "registration_key": "api-project-key-001"})
        project_path = "/api/v1/logical-projects"
        project_response = self.client.post(
            project_path, content=project_body,
            headers={**self.operator_headers, "Content-Type": "application/json"},
        )
        self.assertEqual(project_response.status_code, 201, project_response.text)
        project = project_response.json()
        self.assertEqual(project["schema_version"], 1)

        device_id, credential, key = self._enroll()
        binding_body = canonical_json({"device_id": device_id, "name": "Test laptop"})
        binding_path = f"/api/v1/logical-projects/{project['id']}/bindings"
        binding_response = self.client.post(
            binding_path, content=binding_body,
            headers={**self.operator_headers, "Content-Type": "application/json"},
        )
        self.assertEqual(binding_response.status_code, 201, binding_response.text)
        binding = binding_response.json()

        code = b"def immutable_source():\n    return 'snapshot'\n"
        upload_manifest = [{
            "path": "src/core.py", "file_type": "regular", "size_bytes": len(code),
            "sha256": hashlib.sha256(code).hexdigest(),
        }]
        upload_body = canonical_json({
            "project_id": project["id"], "binding_id": binding["id"],
            "idempotency_key": "api-upload-key-0000001", "files": upload_manifest,
        })
        upload_path = f"/api/v1/device/{device_id}/snapshot-uploads"
        upload = self.client.post(
            upload_path, content=upload_body,
            headers={
                **self._device_headers(device_id, credential, key, "POST", upload_path, upload_body),
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]
        status_path = f"/api/v1/device/{device_id}/snapshot-uploads/{upload_id}"
        status = self.client.get(
            status_path,
            headers=self._device_headers(device_id, credential, key, "GET", status_path, b""),
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["state"], "receiving")
        chunk_path = (
            f"/api/v1/device/{device_id}/snapshot-uploads/{upload_id}/files/0/chunks/0"
        )
        chunk = self.client.put(
            chunk_path, content=code,
            headers=self._device_headers(device_id, credential, key, "PUT", chunk_path, code),
        )
        self.assertEqual(chunk.status_code, 200, chunk.text)
        duplicate = self.client.put(
            chunk_path, content=code,
            headers=self._device_headers(device_id, credential, key, "PUT", chunk_path, code),
        )
        self.assertTrue(duplicate.json()["duplicate"])
        commit_path = f"/api/v1/device/{device_id}/snapshot-uploads/{upload_id}/commit"
        committed = self.client.post(
            commit_path,
            headers=self._device_headers(device_id, credential, key, "POST", commit_path, b""),
        )
        self.assertEqual(committed.status_code, 200, committed.text)
        repeated_commit = self.client.post(
            commit_path,
            headers=self._device_headers(device_id, credential, key, "POST", commit_path, b""),
        )
        self.assertEqual(repeated_commit.status_code, 200, repeated_commit.text)
        self.assertEqual(repeated_commit.json()["snapshot_id"], committed.json()["snapshot_id"])
        listed = self.client.get(f"/api/v1/logical-projects/{project['id']}/snapshots")
        self.assertEqual(len(listed.json()["snapshots"]), 1)
        self.assertEqual(
            self.client.get(f"/api/v1/logical-projects/{project['id']}/tasks").json(),
            {"tasks": [], "execution_available": False},
        )
        self.assertEqual(
            self.client.get("/api/v1/execution-targets").json(),
            {
                "schema_version": 1,
                "targets": [],
                "execution_available": False,
                "broker_available": False,
            },
        )
        self.assertEqual(
            self.client.get("/api/v1/devices").json()["devices"][0]["id"], device_id,
        )
        self.assertEqual(
            self.client.get("/api/v1/projects").json()["projects"], [],
        )
        self.assertEqual(
            self.client.get("/api/v1/devices").status_code, 200,
        )
        task = self.app.state.services.distributed.create_disabled_task_contract(
            project["id"], committed.json()["snapshot_id"], ("repository-index",),
        )
        tasks = self.client.get(f"/api/v1/logical-projects/{project['id']}/tasks").json()["tasks"]
        self.assertEqual(tasks[0]["task_id"], str(task.task_id))
        self.assertEqual(tasks[0]["state"], "not_enabled")
        self.assertEqual(tasks[0]["lease_generation"], 0)
        self.assertFalse(self.client.post(
            f"/api/v1/device/{'f' * 32}/credential/rotate",
            headers=self._device_headers(
                device_id, credential, key, "POST",
                f"/api/v1/device/{'f' * 32}/credential/rotate", b"",
            ),
        ).is_success)

    def test_principal_separation_no_legacy_mutation_endpoint_or_forged_device(self) -> None:
        project = self.client.get("/api/v1/logical-projects")
        self.assertEqual(project.status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/v1/device/" + "a" * 32 + "/snapshot-uploads",
                json={"project_id": "a" * 32, "binding_id": "a" * 32, "files": []},
            ).status_code,
            401,
        )
        paths = set(self.app.openapi()["paths"])
        self.assertFalse(any("terminal" in path or "execute" in path or "runtime" in path for path in paths))
        self.assertNotIn("/api/v1/projects/{project_id}/files", paths)


if __name__ == "__main__":
    unittest.main()
