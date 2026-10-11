from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path

from synai.broker.protocol import verify_claim
from synai.web.broker_authority import CoreBrokerAuthority
from synai.web.database import MetadataDatabase
from synai.web.distributed import DistributedRegistry
from synai.web.snapshots import SnapshotStore


TOKEN = "operator-test-token-only"
SIGNING_KEY = bytes(range(32))
IMAGE_DIGEST = "a" * 64
PROFILE = "python-small"


class CoreBrokerAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.database = MetadataDatabase(root / "core")
        self.database.initialize()
        self.registry = DistributedRegistry(self.database)
        self.snapshots = SnapshotStore(self.database, self.registry)
        self.snapshots.initialize()
        project = self.registry.create_project("Broker test project", "broker-test-project-1", now=10)
        self.project_id = str(project.project_id)
        self.device_id = secrets.token_hex(16)
        self.binding_id = secrets.token_hex(16)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO paired_devices "
                "(device_id, schema_version, public_key, key_fingerprint, state, protocol_version, "
                "capabilities_json, credential_hash, credential_expires_at, credential_generation, "
                "created_at, authorized_at) VALUES (?, 1, ?, ?, 'authorized', 1, '{}', ?, 999999, 1, 1, 1)",
                (self.device_id, b"x" * 32, hashlib.sha256(b"x").hexdigest(), "c" * 64),
            )
            connection.execute(
                "INSERT INTO workspace_bindings "
                "(binding_id, schema_version, project_id, device_id, display_name, state, created_at) "
                "VALUES (?, 1, ?, ?, 'test binding', 'active', 1)",
                (self.binding_id, self.project_id, self.device_id),
            )
        self.snapshot_id = secrets.token_hex(16)
        content = b"before\n"
        self.manifest = {
            "schema_version": 1,
            "project_id": self.project_id,
            "source_device_id": self.device_id,
            "workspace_binding_id": self.binding_id,
            "snapshot_id": self.snapshot_id,
            "created_at": 10,
            "files": [{
                "path": "src/file.txt", "file_type": "regular",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }],
            "total_bytes": len(content),
            "source": {"kind": "paired_device", "key_fingerprint": "d" * 64, "protocol_version": 1},
        }
        self.snapshot_digest = hashlib.sha256(
            json.dumps(self.manifest, sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO immutable_snapshots "
                "(snapshot_id, schema_version, project_id, device_id, binding_id, manifest_json, "
                "manifest_digest, total_bytes, state, created_at, expires_at) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'available', 10, 999999)",
                (
                    self.snapshot_id, self.project_id, self.device_id, self.binding_id,
                    json.dumps(self.manifest, sort_keys=True, separators=(",", ":")),
                    self.snapshot_digest, len(content),
                ),
            )
        snapshot_objects = self.snapshots.snapshots / self.snapshot_id / "objects"
        snapshot_objects.mkdir(mode=0o700, parents=True)
        (snapshot_objects / "0000").write_bytes(content)
        os.chmod(snapshot_objects / "0000", 0o400)
        self.task = self.registry.create_disabled_task_contract(
            self.project_id, self.snapshot_id, now=11,
        )
        self.authority = CoreBrokerAuthority(
            self.database, self.snapshots, SIGNING_KEY,
            deployment_id="e" * 32, integration_test_token=TOKEN,
        )
        self.allocation = self.authority.authorize_integration_task(
            str(self.task.task_id),
            target_id="1" * 32,
            plan_id="2" * 32,
            plan_digest="3" * 64,
            policy_id="4" * 32,
            policy_version=1,
            decision_digest="5" * 64,
            approval_id="6" * 32,
            approval_digest="7" * 64,
            image_digest=IMAGE_DIGEST,
            image_profile_id=PROFILE,
            resource_profile_id=PROFILE,
            allowed_operations=frozenset({"prepare", "collect", "write_file"}),
            operator_token=TOKEN,
            now=12,
        )

    def _claim(self, kind: str, arguments: dict[str, object], operation: str = "8" * 32):
        return self.authority.issue_claim(
            str(self.task.task_id), operation, kind, arguments, now=20,
        )

    def test_authority_gate_claim_signature_replay_revocation_and_fencing(self) -> None:
        with self.assertRaises(Exception):
            self.authority.authorize_integration_task(
                "9" * 32, target_id="1" * 32, plan_id="2" * 32,
                plan_digest="3" * 64, policy_id="4" * 32, policy_version=1,
                decision_digest="5" * 64, approval_id="6" * 32,
                approval_digest="7" * 64, image_digest=IMAGE_DIGEST,
                image_profile_id=PROFILE, resource_profile_id=PROFILE,
                allowed_operations=frozenset({"prepare"}), operator_token="wrong", now=13,
            )
        issued = self._claim("prepare", {})
        verify_claim(issued.claim, issued.signature, self.authority.public_key, 21)
        self.assertEqual(issued.claim.audience, f"synai-broker:{self.allocation}")
        self.assertTrue(self.authority.validate_live_claim(issued.claim, issued.signature, now=21))
        with self.assertRaises(Exception):
            self._claim("prepare", {})
        with self.assertRaises(Exception):
            self.authority.advance_fence(str(self.task.task_id), now=22)
        self.authority.record_operation_state(issued.claim, issued.signature, "failed", now=22)
        self.assertEqual(self.authority.advance_fence(str(self.task.task_id), now=23), 2)
        self.assertFalse(self.authority.validate_live_claim(issued.claim, issued.signature, now=24))

    def test_exact_snapshot_grant_is_scoped_one_use_and_digest_bound(self) -> None:
        issued = self._claim("prepare", {})
        self.assertIsNotNone(issued.snapshot_grant)
        self.authority.mark_operation_running(issued.claim, issued.signature, now=20)
        manifest, files = self.authority.fetch_exact_snapshot(
            issued.claim, issued.signature, issued.snapshot_grant or "", now=21,
        )
        self.assertEqual(manifest["snapshot_id"], self.snapshot_id)
        self.assertEqual(files["src/file.txt"], b"before\n")
        with self.assertRaises(Exception):
            self.authority.fetch_exact_snapshot(
                issued.claim, issued.signature, issued.snapshot_grant or "", now=22,
            )

    def test_core_independently_checks_artifact_bytes_before_durable_result(self) -> None:
        issued = self._claim("collect", {})
        after = b"after\n"
        envelope = {
            "schema_version": 1,
            "claim_id": issued.claim.claim_id,
            "operation_id": issued.claim.operation_id,
            "task_id": issued.claim.task_id,
            "project_id": issued.claim.project_id,
            "snapshot_id": issued.claim.snapshot_id,
            "snapshot_digest": issued.claim.snapshot_digest,
            "allocation_id": issued.claim.allocation_id,
            "lease_generation": issued.claim.lease_generation,
            "status": "completed",
            "result": {
                "changed_files": [{
                    "path": "src/file.txt",
                    "preimage_sha256": self.manifest["files"][0]["sha256"],
                    "postimage_sha256": hashlib.sha256(after).hexdigest(),
                    "size_bytes": len(after),
                    "artifact_base64": base64.b64encode(after).decode("ascii"),
                }],
                "artifact_manifest_sha256": hashlib.sha256(json.dumps(
                    [{
                        "path": "src/file.txt",
                        "preimage_sha256": self.manifest["files"][0]["sha256"],
                        "postimage_sha256": hashlib.sha256(after).hexdigest(),
                        "size_bytes": len(after),
                        "artifact_base64": base64.b64encode(after).decode("ascii"),
                    }],
                    sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest(),
                "collection_complete": True,
            },
        }
        self.assertTrue(self.authority.validate_and_record_result(
            issued.claim, issued.signature, envelope, now=21,
        )["validated"])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT result_digest, result_json FROM broker_task_results WHERE operation_id = ?",
                (issued.claim.operation_id,),
            ).fetchone()
        self.assertEqual(len(row["result_digest"]), 64)
        self.assertIn('"path":"src/file.txt"', row["result_json"])

        bad = self._claim("collect", {}, operation="a" * 32)
        tampered = {**envelope, "claim_id": bad.claim.claim_id, "operation_id": bad.claim.operation_id}
        tampered["result"] = {
            "changed_files": [{
                "path": "src/file.txt", "preimage_sha256": "0" * 64,
                "postimage_sha256": hashlib.sha256(after).hexdigest(), "size_bytes": len(after),
                "artifact_base64": base64.b64encode(after).decode("ascii"),
            }],
        }
        with self.assertRaises(Exception):
            self.authority.validate_and_record_result(bad.claim, bad.signature, tampered, now=21)


if __name__ == "__main__":
    unittest.main()
