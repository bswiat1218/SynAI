from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from synai.broker.app import BrokerService, OperationRequest, create_app
from synai.broker.config import BrokerConfig, BrokerReadiness
from synai.broker.operations import execute_file_operation, validate_operation
from synai.broker.protocol import (
    BrokerError,
    TaskClaim,
    canonical_json,
    request_mac,
    sign_claim,
    verify_claim,
)
from synai.broker.registry import BrokerRegistry
from synai.broker.snapshots import (
    changed_files,
    load_staged_manifest,
    stage_snapshot,
    validate_relative_path,
    validate_snapshot,
)


IMAGE_DIGEST = "sha256:" + "a" * 64
HASH = "b" * 64
KEY = b"service-key-" + b"s" * 32
CLAIM_PRIVATE = bytes(range(32))
CLAIM_PUBLIC = Ed25519PrivateKey.from_private_bytes(CLAIM_PRIVATE).public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw,
)


def config(root: Path, *, podman: str = "podman") -> BrokerConfig:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return BrokerConfig(
        state_dir=root,
        podman=podman,
        image=f"registry.invalid/synai-test@{IMAGE_DIGEST}",
        image_digest=IMAGE_DIGEST,
        core_uid=os.geteuid() + 1,
        service_uid=os.geteuid(),
        service_key=KEY,
        claim_key=CLAIM_PUBLIC,
        socket_gid=os.getgid(),
    )


def claim(
    kind: str,
    arguments: dict[str, object],
    *,
    task_id: str = "1" * 32,
    project_id: str = "2" * 32,
    snapshot_id: str = "3" * 32,
    generation: int = 1,
    now: int | None = None,
) -> TaskClaim:
    current = int(time.time()) if now is None else now
    return TaskClaim(
        schema_version=1,
        task_id=task_id,
        project_id=project_id,
        snapshot_id=snapshot_id,
        snapshot_digest=HASH,
        operation_id=hashlib.md5(f"{kind}:{current}:{os.urandom(4).hex()}".encode()).hexdigest(),
        arguments_digest=hashlib.sha256(canonical_json(arguments)).hexdigest(),
        deadline=current + 120,
        target_id="4" * 32,
        profile="python-small",
        tool_kind=kind,
        plan_digest="5" * 64,
        policy_decision_id="6" * 32,
        policy_version=1,
        approval_id="7" * 32,
        lease_generation=generation,
        fencing_token="f" * 48,
        image_digest=IMAGE_DIGEST.removeprefix("sha256:"),
        resource_profile="python-small",
        issued_at=current,
    )


class FakeSnapshotRetriever:
    def __init__(self, project_id: str = "2" * 32) -> None:
        self.content = b"original\n"
        self.project_id = project_id
        self.manifest: dict[str, object] = {}
        self.digest = ""

    def prepare_manifest(self, snapshot_id: str) -> str:
        self.manifest = {
            "schema_version": 1,
            "project_id": self.project_id,
            "source_device_id": "8" * 32,
            "workspace_binding_id": "9" * 32,
            "snapshot_id": snapshot_id,
            "created_at": int(time.time()),
            "files": [{
                "path": "src/file.txt", "file_type": "regular",
                "size_bytes": len(self.content),
                "sha256": hashlib.sha256(self.content).hexdigest(),
            }],
            "total_bytes": len(self.content),
            "source": {
                "kind": "paired_device",
                "key_fingerprint": "a" * 64,
                "protocol_version": 1,
            },
        }
        self.digest = hashlib.sha256(canonical_json(self.manifest)).hexdigest()
        return self.digest

    async def fetch_exact(self, *, task_id: str, project_id: str, snapshot_id: str,
                          snapshot_digest: str, deadline: int) -> tuple[dict[str, object], dict[str, bytes]]:
        del task_id, deadline
        if not self.manifest or self.manifest["snapshot_id"] != snapshot_id:
            self.prepare_manifest(snapshot_id)
        if snapshot_digest != self.digest or project_id != self.project_id:
            raise BrokerError("snapshot_not_found", "Exact immutable snapshot was not found.", 404)
        return self.manifest, {"src/file.txt": self.content}


class BrokerProtocolTests(unittest.TestCase):
    def test_forged_and_expired_task_claims_are_rejected(self) -> None:
        current = int(time.time())
        request = claim("prepare", {}, now=current)
        signature = sign_claim(request, CLAIM_PRIVATE)
        with self.assertRaises(BrokerError):
            verify_claim(request, "0" * 128, CLAIM_PUBLIC, current)
        with self.assertRaises(BrokerError):
            verify_claim(request, signature, CLAIM_PUBLIC, request.deadline)
        with self.assertRaises(BrokerError):
            TaskClaim.from_dict({**request.to_dict(), "unknown": "field"})

    def test_claim_requires_supported_version_and_complete_bindings(self) -> None:
        current = int(time.time())
        request = claim("prepare", {}, now=current)
        with self.assertRaises(BrokerError):
            TaskClaim.from_dict({**request.to_dict(), "schema_version": 2}).validate(current)
        with self.assertRaises(BrokerError):
            TaskClaim.from_dict({**request.to_dict(), "lease_generation": True}).validate(current)

    def test_service_request_mac_is_bound_to_exact_path_and_body(self) -> None:
        body = b'{"schema_version":1}'
        signature = request_mac("POST", "/v1/tasks", 100, "a" * 32, body, KEY)
        self.assertNotEqual(signature, request_mac("POST", "/v1/other", 100, "a" * 32, body, KEY))
        self.assertNotEqual(signature, request_mac("POST", "/v1/tasks", 100, "a" * 32, b"{}", KEY))

    def test_registry_rejects_operation_replay_and_stale_or_cross_project_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = BrokerRegistry(config(Path(directory) / "broker"))
            registry.initialize()
            prepared = claim("prepare", {})
            signature = sign_claim(prepared, CLAIM_PRIVATE)
            registry.authorize(prepared, signature)
            with self.assertRaises(BrokerError) as replay:
                registry.authorize(prepared, signature)
            self.assertEqual(replay.exception.code, "operation_replayed")
            changed = claim("status", {}, project_id="c" * 32)
            with self.assertRaises(BrokerError) as cross_project:
                registry.authorize(changed, sign_claim(changed, CLAIM_PRIVATE))
            self.assertEqual(cross_project.exception.code, "task_binding_mismatch")
            stale = claim("status", {}, generation=2)
            with self.assertRaises(BrokerError) as stale_claim:
                registry.authorize(stale, sign_claim(stale, CLAIM_PRIVATE))
            self.assertEqual(stale_claim.exception.code, "fence_update_required")

    def test_request_nonce_is_persistently_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = BrokerRegistry(config(Path(directory) / "broker"))
            registry.initialize()
            registry.consume_request_nonce("a" * 32, 100)
            with self.assertRaises(BrokerError) as replay:
                registry.consume_request_nonce("a" * 32, 101)
            self.assertEqual(replay.exception.code, "request_replayed")

    def test_recovery_marks_running_work_uncertain_and_quarantines_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = BrokerRegistry(config(Path(directory) / "broker"))
            registry.initialize()
            prepared = claim("prepare", {})
            registry.authorize(prepared, sign_claim(prepared, CLAIM_PRIVATE))
            registry.transition(prepared.task_id, "ready")
            registry.reserve_container(prepared.task_id, "d" * 32)
            registry.bind_container(prepared.task_id, "d" * 32, "e" * 64)
            registry.transition(prepared.task_id, "running")
            restarted = BrokerRegistry(registry.config)
            restarted.initialize()
            self.assertEqual(restarted.task(prepared.task_id)["state"], "uncertain")
            with self.assertRaises(BrokerError) as quarantined:
                restarted.owned_container(prepared.task_id)
            self.assertEqual(quarantined.exception.code, "container_quarantined")


class SnapshotStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.content = b"module = True\n"
        self.manifest = {
            "schema_version": 1,
            "project_id": "2" * 32,
            "source_device_id": "8" * 32,
            "workspace_binding_id": "9" * 32,
            "snapshot_id": "3" * 32,
            "created_at": 1,
            "files": [{
                "path": "src/module.py", "file_type": "regular",
                "size_bytes": len(self.content),
                "sha256": hashlib.sha256(self.content).hexdigest(),
            }],
            "total_bytes": len(self.content),
            "source": {
                "kind": "paired_device", "key_fingerprint": "a" * 64,
                "protocol_version": 1,
            },
        }
        self.digest = hashlib.sha256(canonical_json(self.manifest)).hexdigest()

    def test_integrity_validation_and_writable_copy_preserve_snapshot(self) -> None:
        validate_snapshot(self.manifest, "3" * 32, self.digest, {"src/module.py": self.content})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            root.mkdir(mode=0o700)
            workspace = stage_snapshot(root, "1" * 32, self.manifest, {"src/module.py": self.content})
            self.assertEqual((workspace / "src/module.py").read_bytes(), self.content)
            (workspace / "src/module.py").write_bytes(b"changed\n")
            self.assertEqual((root / "tasks" / ("1" * 32) / "snapshot" / "src/module.py").read_bytes(), self.content)
            staged = load_staged_manifest(root, "1" * 32, self.digest)
            changes = changed_files(workspace, staged)
            self.assertEqual(changes[0]["preimage_sha256"], hashlib.sha256(self.content).hexdigest())
            self.assertEqual(changes[0]["postimage_sha256"], hashlib.sha256(b"changed\n").hexdigest())

    def test_manifest_tampering_and_identity_mismatch_are_rejected(self) -> None:
        with self.assertRaises(BrokerError):
            validate_snapshot(self.manifest, "c" * 32, self.digest, {"src/module.py": self.content})
        with self.assertRaises(BrokerError):
            validate_snapshot(self.manifest, "3" * 32, "0" * 64, {"src/module.py": self.content})
        tampered = dict(self.manifest)
        tampered["project_id"] = "f" * 32
        with self.assertRaises(BrokerError):
            validate_snapshot(tampered, "3" * 32, self.digest, {"src/module.py": self.content})

    def test_path_traversal_collision_and_symlink_are_rejected(self) -> None:
        for path in ("../outside", "/etc/passwd", "a\\b", "a/./b", "a\x00b"):
            with self.subTest(path=path), self.assertRaises(BrokerError):
                validate_relative_path(path)
        collision = {**self.manifest}
        collision["files"] = [
            *self.manifest["files"],
            {**self.manifest["files"][0], "path": "SRC/module.py"},
        ]
        collision["total_bytes"] *= 2
        with self.assertRaises(BrokerError):
            validate_snapshot(collision, "3" * 32, hashlib.sha256(canonical_json(collision)).hexdigest(),
                             {"src/module.py": self.content, "SRC/module.py": self.content})
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            (workspace / "outside").symlink_to(Path(directory))
            with self.assertRaises(BrokerError):
                changed_files(workspace, self.manifest)

    def test_snapshot_content_digest_and_bounds_are_checked(self) -> None:
        with self.assertRaises(BrokerError):
            validate_snapshot(self.manifest, "3" * 32, self.digest, {"src/module.py": b"tampered"})
        entry = {**self.manifest["files"][0], "file_type": "symlink"}
        invalid = {**self.manifest, "files": [entry]}
        invalid["files"] = [entry]
        invalid["total_bytes"] = len(self.content)
        with self.assertRaises(BrokerError):
            validate_snapshot(invalid, "3" * 32, hashlib.sha256(canonical_json(invalid)).hexdigest(),
                              {"src/module.py": self.content})


class TypedOperationTests(unittest.TestCase):
    def test_workspace_file_mutations_are_scoped_and_preimage_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            original = b"before\n"
            (root / "file.txt").write_bytes(original)
            old_digest = hashlib.sha256(original).hexdigest()
            result = execute_file_operation(
                root, "patch_file",
                {"path": "file.txt", "old": "before", "new": "after",
                 "expected_sha256": old_digest},
                1024,
            )
            self.assertEqual((root / "file.txt").read_bytes(), b"after\n")
            self.assertEqual(result["preimage_sha256"], old_digest)
            with self.assertRaises(BrokerError) as conflict:
                execute_file_operation(
                    root, "delete_file",
                    {"path": "file.txt", "expected_sha256": old_digest},
                    1024,
                )
            self.assertEqual(conflict.exception.code, "preimage_conflict")

    def test_file_operations_reject_symlinks_special_files_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            (root / "escape").symlink_to(Path(directory))
            with self.assertRaises(BrokerError):
                execute_file_operation(root, "read_file", {"path": "escape/secret"}, 1024)
            with self.assertRaises(BrokerError):
                execute_file_operation(root, "read_file", {"path": "../secret"}, 1024)

    def test_unapproved_tool_shapes_mounts_images_and_oversized_commands_fail(self) -> None:
        with self.assertRaises(BrokerError):
            validate_operation("terminal", {"command": "id"})
        with self.assertRaises(BrokerError):
            validate_operation("command", {"argv": ["python3", "x"], "cwd": ".", "mount": "/"})
        with self.assertRaises(BrokerError):
            validate_operation("command", {"argv": ["python3", "x" * 5000], "cwd": "."})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                BrokerConfig(
                    state_dir=Path(directory), podman="podman", image="unapproved:latest",
                    image_digest=IMAGE_DIGEST, core_uid=1, service_uid=2,
                    service_key=KEY, claim_key=CLAIM_PUBLIC, socket_gid=os.getgid(),
                ).validate()

    def test_runtime_and_storage_limits_are_fail_closed_without_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = config(Path(directory) / "broker", podman="absent-podman-command")
            selected.state_dir.mkdir(mode=0o700, exist_ok=True)
            readiness = selected.readiness()
            self.assertFalse(readiness.ready)
            self.assertFalse(readiness.execution_available)
            self.assertIn("workspace_quota", readiness.reasons)
            with self.assertRaises(ValueError):
                BrokerConfig(
                    state_dir=selected.state_dir, podman="podman", image=selected.image,
                    image_digest=selected.image_digest, core_uid=1, service_uid=1,
                    service_key=KEY, claim_key=CLAIM_PUBLIC, socket_gid=os.getgid(),
                ).validate()


class BrokerServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "broker"
        self.selected = config(self.root)
        self.snapshots = FakeSnapshotRetriever()
        self.service = BrokerService(
            self.selected,
            snapshot_retriever=self.snapshots,
            lease_validator=_valid_lease,
        )
        self.service.registry.initialize()
        self.service.readiness = BrokerReadiness(
            False, False, {"workspace_quota": False}, ("workspace_quota",),
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_prepare_and_scoped_read_uses_exact_immutable_snapshot(self) -> None:
        initial = claim("prepare", {})
        digest = self.snapshots.prepare_manifest(initial.snapshot_id)
        prepare = replace(initial, snapshot_digest=digest)
        prepared = await self.service.dispatch(OperationRequest(
            schema_version=1, claim=prepare.to_dict(),
            claim_signature=sign_claim(prepare, CLAIM_PRIVATE), kind="prepare", arguments={},
        ))
        self.assertEqual(prepared["result"]["state"], "ready")
        read_args = {"path": "src/file.txt"}
        read = replace(
            prepare,
            operation_id="a" * 32,
            tool_kind="read_file",
            arguments_digest=hashlib.sha256(canonical_json(read_args)).hexdigest(),
        )
        response = await self.service.dispatch(OperationRequest(
            schema_version=1, claim=read.to_dict(),
            claim_signature=sign_claim(read, CLAIM_PRIVATE),
            kind="read_file", arguments=read_args,
        ))
        self.assertEqual(response["result"]["content"], self.snapshots.content.decode())
        self.assertEqual(response["result"]["sha256"], hashlib.sha256(self.snapshots.content).hexdigest())

    async def test_failed_snapshot_retrieval_leaves_task_cleanable(self) -> None:
        class MissingSnapshot:
            async def fetch_exact(self, **_: object) -> tuple[dict[str, object], dict[str, bytes]]:
                raise BrokerError("snapshot_not_found", "Exact snapshot unavailable.", 404)

        service = BrokerService(
            self.selected, snapshot_retriever=MissingSnapshot(), lease_validator=_valid_lease,
        )
        service.registry.initialize()
        request = claim("prepare", {})
        payload = OperationRequest(
            schema_version=1, claim=request.to_dict(),
            claim_signature=sign_claim(request, CLAIM_PRIVATE), kind="prepare", arguments={},
        )
        with self.assertRaises(BrokerError):
            await service.dispatch(payload)
        self.assertEqual(service.registry.task(request.task_id)["state"], "failed")

        remove = replace(
            request,
            operation_id="c" * 32,
            tool_kind="remove",
            arguments_digest=hashlib.sha256(canonical_json({})).hexdigest(),
        )
        result = await service.dispatch(OperationRequest(
            schema_version=1, claim=remove.to_dict(),
            claim_signature=sign_claim(remove, CLAIM_PRIVATE), kind="remove", arguments={},
        ))
        self.assertEqual(result["result"]["state"], "removed")

    async def test_cancel_before_start_removes_only_its_prepared_workspace(self) -> None:
        first = claim("prepare", {})
        digest = self.snapshots.prepare_manifest(first.snapshot_id)
        prepared = replace(first, snapshot_digest=digest)
        await self.service.dispatch(OperationRequest(
            schema_version=1, claim=prepared.to_dict(),
            claim_signature=sign_claim(prepared, CLAIM_PRIVATE),
            kind="prepare", arguments={},
        ))
        other = self.root / "operator-data.txt"
        other.write_text("preserve")
        cancel = replace(
            prepared,
            operation_id="d" * 32,
            tool_kind="cancel",
            arguments_digest=hashlib.sha256(canonical_json({})).hexdigest(),
        )
        result = await self.service.dispatch(OperationRequest(
            schema_version=1, claim=cancel.to_dict(),
            claim_signature=sign_claim(cancel, CLAIM_PRIVATE),
            kind="cancel", arguments={},
        ))
        self.assertEqual(result["result"]["state"], "removed")
        self.assertFalse((self.root / "tasks" / prepared.task_id).exists())
        self.assertEqual(other.read_text(), "preserve")
        self.assertEqual(self.service.registry.task(prepared.task_id)["state"], "removed")

    async def test_missing_live_lease_authority_fails_closed(self) -> None:
        service = BrokerService(self.selected, snapshot_retriever=self.snapshots)
        service.registry.initialize()
        request = claim("prepare", {})
        payload = OperationRequest(
            schema_version=1, claim=request.to_dict(),
            claim_signature=sign_claim(request, CLAIM_PRIVATE), kind="prepare", arguments={},
        )
        with self.assertRaises(BrokerError) as unavailable:
            await service.dispatch(payload)
        self.assertEqual(unavailable.exception.code, "lease_authority_unavailable")

    async def test_claimed_operation_recovery_never_replays_mutation(self) -> None:
        prepared = claim("prepare", {})
        self.service.registry.authorize(prepared, sign_claim(prepared, CLAIM_PRIVATE))
        self.service.registry.transition(prepared.task_id, "ready")
        operation = claim("write_file", {
            "path": "src/file.txt", "content": "new", "expected_sha256": None,
        })
        self.service.registry.authorize(operation, sign_claim(operation, CLAIM_PRIVATE))
        BrokerRegistry(self.selected).initialize()
        with self.assertRaises(BrokerError) as quarantined:
            self.service.registry.authorize(operation, sign_claim(operation, CLAIM_PRIVATE))
        self.assertEqual(quarantined.exception.code, "operation_replayed")
        self.assertEqual(self.service.registry.task(prepared.task_id)["state"], "uncertain")


class BrokerHTTPAuthenticationTests(unittest.TestCase):
    def test_forged_and_replayed_service_requests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = config(Path(directory) / "broker")
            service = BrokerService(selected)
            service.registry.initialize()
            app = create_app(service)
            client = TestClient(app)
            timestamp = int(time.time())
            nonce = "a" * 32
            signature = request_mac("GET", "/v1/ready", timestamp, nonce, b"", KEY)
            headers = {
                "X-SynAI-Broker-Timestamp": str(timestamp),
                "X-SynAI-Broker-Nonce": nonce,
                "X-SynAI-Broker-Signature": signature,
            }
            self.assertEqual(client.get("/v1/ready", headers=headers).status_code, 200)
            replay = client.get("/v1/ready", headers=headers)
            self.assertEqual(replay.status_code, 409)
            forged = client.get("/v1/ready", headers={**headers, "X-SynAI-Broker-Signature": "0" * 64})
            self.assertEqual(forged.status_code, 401)

    def test_unsupported_request_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = config(Path(directory) / "broker")
            service = BrokerService(selected)
            service.registry.initialize()
            client = TestClient(create_app(service))
            body = json.dumps({
                "schema_version": 2, "claim": {}, "claim_signature": "0" * 128,
                "kind": "prepare", "arguments": {},
            }, separators=(",", ":")).encode()
            timestamp = int(time.time())
            nonce = "c" * 32
            headers = {
                "content-type": "application/json",
                "X-SynAI-Broker-Timestamp": str(timestamp),
                "X-SynAI-Broker-Nonce": nonce,
                "X-SynAI-Broker-Signature": request_mac(
                    "POST", "/v1/tasks/" + "1" * 32 + "/operations",
                    timestamp, nonce, body, KEY,
                ),
            }
            response = client.post(
                "/v1/tasks/" + "1" * 32 + "/operations",
                content=body, headers=headers,
            )
            self.assertEqual(response.status_code, 422)


async def _valid_lease(_: TaskClaim) -> bool:
    return True


if __name__ == "__main__":
    unittest.main()
