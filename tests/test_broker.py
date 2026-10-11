from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import socket
import tempfile
import time
import unittest
import asyncio
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from synai.broker.app import BrokerService, OperationRequest, create_app
from synai.broker.config import BrokerConfig, BrokerReadiness, QuotaSlot, StorageRole
from synai.broker.operations import execute_file_operation, validate_operation
from synai.broker.protocol import (
    BrokerError,
    TaskClaim,
    canonical_json,
    sign_claim,
    verify_claim,
)
from synai.broker.registry import BrokerRegistry
from synai.broker import runtime as runtime_module
from synai.broker.runtime import RootlessPodmanRuntime
from synai.broker.quota_slots import QuotaUsage, aggregate_storage_reasons
from synai.broker import quota_slots as quota_slots_module
from synai.broker.quota_slots import QuotaSlotManager
from synai.broker.storage_inspector import (
    InspectorInvalid,
    InspectorStale,
    InspectorUnavailable,
    StorageInspectionRequest,
    StorageObservation,
    UnixStorageInspectorClient,
    inventory_generation,
)
from synai.broker.snapshots import (
    changed_files,
    load_staged_manifest,
    stage_snapshot,
    validate_relative_path,
    validate_snapshot,
)


IMAGE_DIGEST = "sha256:" + "a" * 64
HASH = "b" * 64
CLAIM_PRIVATE = bytes(range(32))
CLAIM_PUBLIC = Ed25519PrivateKey.from_private_bytes(CLAIM_PRIVATE).public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw,
)


def config(root: Path, *, podman: str = "podman", slot_count: int = 1) -> BrokerConfig:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    mount_path = root.parent / "quota-volume"
    tasks_path = mount_path / "tasks"
    tasks_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    slots = tuple(
        QuotaSlot(
            slot_id=f"slot-{index:02d}",
            path=tasks_path / f"slot-{index:02d}",
            mount_path=mount_path,
            filesystem_uuid="11111111-1111-4111-8111-111111111111",
            project_id=29001 + index,
        )
        for index in range(slot_count)
    )
    for slot in slots:
        slot.path.mkdir(mode=0o700, exist_ok=True)
    role_data = (
        ("podman_graph", "podman-graph", mount_path / "podman" / "graph", 4 * 1024**3, 131_072),
        ("podman_runtime", "podman-runtime", mount_path / "podman" / "run", 512 * 1024**2, 16_384),
        ("image_scratch", "image-scratch", mount_path / "podman" / "scratch", 512 * 1024**2, 16_384),
        ("broker_state", "broker-state", mount_path / "broker-state", 512 * 1024**2, 16_384),
        ("broker_logs", "broker-logs", mount_path / "broker-logs", 512 * 1024**2, 8192),
        ("temporary", "broker-temp", mount_path / "temporary", 512 * 1024**2, 16_384),
    )
    storage_roles: list[StorageRole] = []
    for slot in slots:
        storage_roles.append(StorageRole(
            role_id=f"task-{slot.slot_id}", kind="task_slot", path=slot.path,
            mount_path=mount_path, filesystem_uuid=slot.filesystem_uuid,
            project_id=slot.project_id, byte_limit=slot.byte_limit,
            inode_limit=slot.inode_limit, owner_uid=os.geteuid(), owner_gid=os.getegid(),
        ))
    for index, (kind, role_id, path, byte_limit, inode_limit) in enumerate(role_data):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        storage_roles.append(StorageRole(
            role_id=role_id, kind=kind, path=path, mount_path=mount_path,
            filesystem_uuid="11111111-1111-4111-8111-111111111111",
            project_id=29100 + index, byte_limit=byte_limit,
            inode_limit=inode_limit, owner_uid=os.geteuid(), owner_gid=os.getegid(),
        ))
    broker_state = next(role.path for role in storage_roles if role.kind == "broker_state")
    return BrokerConfig(
        state_dir=broker_state,
        podman=podman,
        image=f"registry.invalid/synai-test@{IMAGE_DIGEST}",
        image_digest=IMAGE_DIGEST,
        core_uid=os.geteuid() + 1,
        service_uid=os.geteuid(),
        claim_key=CLAIM_PUBLIC,
        core_ca_file=root / "core-ca.pem",
        broker_cert_file=root / "broker.pem",
        broker_key_file=root / "broker.key",
        quota_slots=slots,
        storage_roles=tuple(storage_roles),
    )


class FakeQuotaInspector:
    def inspect(self, role_id: str) -> QuotaUsage:
        del role_id
        return QuotaUsage(1, 1, 0, 0)


class FakeQuotaManager:
    def __init__(self, selected: BrokerConfig, registry: BrokerRegistry) -> None:
        self.config = selected
        self.registry = registry
        self.inspector = FakeQuotaInspector()
        self.verified = True
        self.verified_slot_ids = frozenset(slot.slot_id for slot in selected.quota_slots)
        self.runtime_containment_available = True

    def verify_startup(self) -> tuple[bool, tuple[str, ...]]:
        return True, ()

    def resolve_task_root(self, task_id: str, *, allow_quarantined: bool = False) -> Path:
        assignment = self.registry.task_slot(task_id, allow_quarantined=allow_quarantined)
        slot = next(slot for slot in self.config.quota_slots if slot.slot_id == assignment["slot_id"])
        return slot.path / task_id

    def verify_releasable(self, task_id: str) -> Path:
        return self.resolve_task_root(task_id, allow_quarantined=True)

    def create_task_root(self, task_id: str) -> Path:
        task_root = self.resolve_task_root(task_id)
        task_root.mkdir(mode=0o700)
        return task_root

    def recover_pending_releases(self) -> None:
        return None


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
    operation_id = hashlib.md5(f"{kind}:{current}:{os.urandom(4).hex()}".encode()).hexdigest()
    claim_id = hashlib.sha256(f"claim:{operation_id}".encode()).hexdigest()[:32]
    request_id = hashlib.sha256(f"request:{operation_id}".encode()).hexdigest()[:32]
    replay_nonce = hashlib.sha256(f"nonce:{operation_id}".encode()).hexdigest()[:32]
    return TaskClaim(
        schema_version=1,
        task_id=task_id,
        project_id=project_id,
        snapshot_id=snapshot_id,
        snapshot_digest=HASH,
        operation_id=operation_id,
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
        claim_id=claim_id,
        issuer="synai-core:" + "9" * 32,
        audience="synai-broker:" + "a" * 32,
        allocation_id="a" * 32,
        plan_id="b" * 32,
        policy_id="c" * 32,
        decision_digest="d" * 64,
        approval_digest="e" * 64,
        replay_nonce=replay_nonce,
        request_id=request_id,
    )


def _observation_wire(
    request: dict[str, object],
    role: StorageRole,
    changes: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "message_type": "storage_inspection_result",
        "role_id": role.role_id,
        "challenge": request["challenge"],
        "status": "ok",
        "helper_boot_epoch": "a" * 32,
        "inventory_generation": request["inventory_generation"],
        "observed_at": int(time.time()),
        "filesystem_uuid": role.filesystem_uuid,
        "mount_target": str(role.mount_path),
        "mount_id": 10,
        "filesystem_type": "ext4",
        "quota_mode": "project",
        "quota_configuration_enabled": True,
        "project_id": role.project_id,
        "inheritance_enabled": True,
        "byte_limit": role.byte_limit,
        "inode_limit": role.inode_limit,
        "bytes_used": 0,
        "inodes_used": 0,
        "filesystem_bytes": 16 * 1024**3,
        "filesystem_bytes_available": 16 * 1024**3,
        "filesystem_inodes": 262_144,
        "filesystem_inodes_available": 262_144,
        "owner_uid": role.owner_uid,
        "owner_gid": role.owner_gid,
    }
    value.update(changes or {})
    return value


class _InspectorTestServer:
    def __init__(
        self,
        path: Path,
        *,
        uid: int,
        gid: int,
        role: StorageRole,
        generation: str,
        response_factory: object,
    ) -> None:
        import threading

        self.path = path
        self.uid = uid
        self.gid = gid
        self.role = role
        self.generation = generation
        self.response_factory = response_factory
        self.listener: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()

    def start(self) -> None:
        import threading

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.listener.listen(4)
        self.listener.settimeout(0.05)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.listener is not None:
            self.listener.close()
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.path.unlink(missing_ok=True)

    def _serve(self) -> None:
        assert self.listener is not None
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                try:
                    header = _recv_exact(connection, 4)
                    size = int.from_bytes(header, "big")
                    request = json.loads(_recv_exact(connection, size))
                    if (
                        request.get("role_id") != self.role.role_id
                        or request.get("inventory_generation") != self.generation
                    ):
                        continue
                    result = self.response_factory(request, self.role)
                    if not isinstance(result, dict):
                        continue
                    encoded = json.dumps(result, separators=(",", ":")).encode()
                    connection.sendall(len(encoded).to_bytes(4, "big") + encoded)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise OSError("socket closed")
        result.extend(chunk)
    return bytes(result)


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

    async def fetch_exact(self, *, claim: TaskClaim, signature: str,
                          grant_token: str) -> tuple[dict[str, object], dict[str, bytes]]:
        del signature, grant_token
        task_id, project_id, snapshot_id = claim.task_id, claim.project_id, claim.snapshot_id
        snapshot_digest = claim.snapshot_digest
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


class QuotaSlotVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "broker"
        self.selected = config(self.root)
        self.registry = BrokerRegistry(self.selected)
        self.registry.initialize()
        self.slot = self.selected.quota_slots[0]
        self._production_test_inspector = None

    def tearDown(self) -> None:
        if self._production_test_inspector is not None:
            self._production_test_inspector.stop()
        self.temporary.cleanup()

    def production_client(self, response_factory: object) -> UnixStorageInspectorClient:
        if self._production_test_inspector is not None:
            self._production_test_inspector.stop()
        role = self.selected.storage_roles[0]
        generation = inventory_generation(self.selected.storage_roles)
        self._production_test_inspector = _InspectorTestServer(
            self.root / "inspector.sock",
            uid=self.selected.inspector_uid,
            gid=self.selected.inspector_gid,
            role=role,
            generation=generation,
            response_factory=response_factory,
        )
        self._production_test_inspector.start()
        client = UnixStorageInspectorClient(
            self._production_test_inspector.path,
            self.selected.storage_roles,
            inspector_uid=os.geteuid(),
            inspector_gid=os.getegid(),
            timeout_seconds=0.25,
            max_age_seconds=5,
            inventory_generation=generation,
        )
        return client

    def test_typed_inspector_accepts_bound_fresh_response(self) -> None:
        client = self.production_client(lambda request, role: _observation_wire(request, role))
        value = client.inspect(self.selected.storage_roles[0].role_id)
        self.assertEqual(value.role_id, self.selected.storage_roles[0].role_id)

    def test_typed_inspector_rejects_challenge_generation_and_stale_response(self) -> None:
        for mutation, error in (
            ({"challenge": "0" * 64}, InspectorInvalid),
            ({"inventory_generation": "0" * 64}, InspectorInvalid),
            ({"observed_at": int(time.time()) - 60}, InspectorStale),
        ):
            with self.subTest(mutation=mutation):
                client = self.production_client(
                    lambda request, role, mutation=mutation: _observation_wire(
                        request, role, mutation,
                    ),
                )
                with self.assertRaises(error):
                    client.inspect(self.selected.storage_roles[0].role_id)

    def test_typed_inspector_rejects_wrong_peer_and_unconfigured_role(self) -> None:
        client = self.production_client(lambda request, role: _observation_wire(request, role))
        with patch.object(
            client, "_verify_peer",
            side_effect=InspectorInvalid("simulated unauthorized peer"),
        ), self.assertRaises(InspectorInvalid):
            client.inspect(self.selected.storage_roles[0].role_id)
        with self.assertRaises(InspectorInvalid):
            client.inspect("../etc")

    def test_typed_request_parser_rejects_unapproved_role_and_extra_path_fields(self) -> None:
        role_id = self.selected.storage_roles[0].role_id
        generation = inventory_generation(self.selected.storage_roles)
        request = StorageInspectionRequest.issue(role_id, generation)
        parsed = StorageInspectionRequest.parse(
            request.to_wire(),
            approved_roles=frozenset({role_id}),
            expected_generation=generation,
        )
        self.assertEqual(parsed.role_id, role_id)
        with self.assertRaises(InspectorInvalid):
            StorageInspectionRequest.parse(
                {**request.to_wire(), "path": "/etc"},
                approved_roles=frozenset({role_id}),
                expected_generation=generation,
            )
        with self.assertRaises(InspectorInvalid):
            StorageInspectionRequest.parse(
                {**request.to_wire(), "role_id": "unapproved"},
                approved_roles=frozenset({role_id}),
                expected_generation=generation,
            )

    def test_typed_inspector_reports_timeout_and_replay(self) -> None:
        server = _InspectorTestServer(
            self.root / "slow.sock",
            uid=os.geteuid(), gid=os.getegid(),
            role=self.selected.storage_roles[0],
            generation=inventory_generation(self.selected.storage_roles),
            response_factory=lambda request, role: time.sleep(0.5),
        )
        self._production_test_inspector = server
        server.start()
        client = UnixStorageInspectorClient(
            server.path, self.selected.storage_roles,
            inspector_uid=os.geteuid(), inspector_gid=os.getegid(),
            timeout_seconds=0.05, max_age_seconds=5,
            inventory_generation=inventory_generation(self.selected.storage_roles),
        )
        with self.assertRaises(InspectorUnavailable):
            client.inspect(self.selected.storage_roles[0].role_id)

    def test_typed_inspector_rejects_replayed_response_and_boot_epoch_change(self) -> None:
        role = self.selected.storage_roles[0]
        replayed: dict[str, object] = {}

        def replay_response(request: dict[str, object], selected_role: StorageRole) -> dict[str, object]:
            if not replayed:
                replayed.update(_observation_wire(request, selected_role))
            return dict(replayed)

        client = self.production_client(replay_response)
        client.inspect(role.role_id)
        with self.assertRaises(InspectorInvalid):
            client.inspect(role.role_id)

        def epoch_response(request: dict[str, object], selected_role: StorageRole) -> dict[str, object]:
            epoch = "a" * 32 if not replayed.get("second_epoch") else "b" * 32
            value = _observation_wire(request, selected_role, {"helper_boot_epoch": epoch})
            replayed["second_epoch"] = True
            return value

        replayed.clear()
        client = self.production_client(epoch_response)
        client.inspect(role.role_id)
        with self.assertRaises(InspectorStale):
            client.inspect(role.role_id)

    def manager(self, changed_role: str | None = None, change: dict[str, object] | None = None) -> QuotaSlotManager:
        class Inspector:
            def inspect(inner_self, role_id: str) -> StorageObservation:
                role = next(item for item in self.selected.storage_roles if item.role_id == role_id)
                values: dict[str, object] = {
                    "role_id": role.role_id,
                    "helper_boot_epoch": "a" * 32,
                    "inventory_generation": inventory_generation(self.selected.storage_roles),
                    "observed_at": int(time.time()),
                    "filesystem_uuid": role.filesystem_uuid,
                    "mount_target": str(role.mount_path),
                    "mount_id": 10,
                    "filesystem_type": "ext4",
                    "quota_mode": "project",
                    "quota_configuration_enabled": True,
                    "project_id": role.project_id,
                    "inheritance_enabled": True,
                    "byte_limit": role.byte_limit,
                    "inode_limit": role.inode_limit,
                    "bytes_used": 0,
                    "inodes_used": 0,
                    "filesystem_bytes": self.selected.quota_volume_bytes,
                    "filesystem_bytes_available": self.selected.quota_volume_bytes,
                    "filesystem_inodes": self.selected.quota_volume_inodes,
                    "filesystem_inodes_available": self.selected.quota_volume_inodes,
                    "owner_uid": role.owner_uid,
                    "owner_gid": role.owner_gid,
                }
                if role_id == changed_role and change:
                    values.update(change)
                return StorageObservation(**values)  # type: ignore[arg-type]

        return QuotaSlotManager(self.selected, self.registry, Inspector())

    def verify_manager(self, manager: QuotaSlotManager) -> tuple[bool, tuple[str, ...]]:
        with patch.object(quota_slots_module, "_process_identity_is_restricted", return_value=True):
            return manager.verify_startup()

    def test_mock_observations_never_enable_production_readiness(self) -> None:
        manager = self.manager()
        verified, reasons = self.verify_manager(manager)
        self.assertFalse(verified, reasons)
        self.assertIn("storage_inspector_untrusted", reasons)
        self.assertFalse(manager.verified_slot_ids)

    def test_startup_rejects_incorrect_byte_and_inode_limits(self) -> None:
        for change in (
            {"byte_limit": self.slot.byte_limit - 1024},
            {"inode_limit": self.slot.inode_limit - 1},
            {"project_id": self.slot.project_id + 1},
            {"inheritance_enabled": False},
            {"quota_configuration_enabled": False},
        ):
            with self.subTest(change=change):
                manager = self.manager("task-slot-00", change)
                verified, reasons = self.verify_manager(manager)
                self.assertFalse(verified)
                self.assertIn("storage_role_inconsistent:task-slot-00", reasons)
                self.assertFalse(manager.verified_slot_ids)

    def test_startup_fails_closed_when_inspection_is_inconclusive(self) -> None:
        class UnavailableInspector:
            def inspect(inner_self, role_id: str) -> StorageObservation:
                del role_id
                raise BrokerError("workspace_quota_unreadable", "quota query unavailable", 503)

        manager = QuotaSlotManager(self.selected, self.registry, UnavailableInspector())
        verified, reasons = self.verify_manager(manager)
        self.assertFalse(verified)
        self.assertIn("storage_role_inconsistent:task-slot-00", reasons)

    def test_shared_filesystem_with_separate_project_domains_is_valid(self) -> None:
        self.selected.validate()
        manager = self.manager()
        observations = {
            role.role_id: manager.inspector.inspect(role.role_id)
            for role in self.selected.storage_roles
        }
        self.assertEqual(aggregate_storage_reasons(self.selected, observations), ())

    def test_aggregate_headroom_accounts_for_every_remaining_role_entitlement(self) -> None:
        manager = self.manager()
        baseline = {
            role.role_id: manager.inspector.inspect(role.role_id)
            for role in self.selected.storage_roles
        }
        role = self.selected.storage_roles[0]
        for field in ("filesystem_bytes_available", "filesystem_inodes_available"):
            with self.subTest(field=field):
                observations = dict(baseline)
                observations[role.role_id] = StorageObservation(
                    **{**baseline[role.role_id].__dict__, field: 1},
                )
                self.assertEqual(
                    aggregate_storage_reasons(self.selected, observations),
                    ("storage_aggregate_headroom",),
                )

    def test_root_backed_storage_role_is_rejected(self) -> None:
        role = next(item for item in self.selected.storage_roles if item.kind == "broker_logs")
        bad = replace(role, mount_path=Path("/"))
        with self.assertRaises(ValueError):
            bad.validate()

    def test_storage_role_config_requires_complete_roles_and_safe_bounds(self) -> None:
        incomplete = replace(self.selected, storage_roles=self.selected.storage_roles[:-1])
        with self.assertRaises(ValueError):
            incomplete.validate()
        root_role = replace(
            self.selected.storage_roles[1],
            path=Path("/tmp/broker-store"), mount_path=Path("/"),
        )
        with self.assertRaises(ValueError):
            root_role.validate()

    def test_inspector_failure_states_have_distinct_readiness_reasons(self) -> None:
        from synai.broker.storage_inspector import InspectorStale

        class StaleInspector:
            def inspect(inner_self, role_id: str) -> StorageObservation:
                raise InspectorStale(f"stale {role_id}")

        manager = QuotaSlotManager(self.selected, self.registry, StaleInspector())
        verified, reasons = self.verify_manager(manager)
        self.assertFalse(verified)
        self.assertTrue(any(reason.startswith("storage_role_stale:") for reason in reasons))

    def test_quota_drift_invalidates_after_a_previous_good_inspection(self) -> None:
        manager = self.manager()
        first, _ = self.verify_manager(manager)
        self.assertFalse(first)
        self.assertFalse(manager.verified_slot_ids)
        drifted = self.manager("task-slot-00", {"byte_limit": self.slot.byte_limit - 4096})
        verified, reasons = self.verify_manager(drifted)
        self.assertFalse(verified)
        self.assertIn("storage_role_inconsistent:task-slot-00", reasons)

    def test_task_root_is_created_before_staging_and_project_inheritance_is_checked(self) -> None:
        request = claim("prepare", {})
        self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        manager = self.manager()
        manager.verified = True
        manager.verified_slot_ids = frozenset({"slot-00"})
        with patch.object(
            quota_slots_module, "_project_attributes",
            return_value=(self.slot.project_id, quota_slots_module._FS_XFLAG_PROJINHERIT),
        ), patch.object(manager, "_verify_local_role_path"):
            task_root = manager.create_task_root(request.task_id)
        self.assertTrue(task_root.is_dir())
        self.assertEqual(task_root.stat().st_uid, self.selected.service_uid)

    def test_symlink_substitution_and_missing_inheritance_quarantine_the_slot(self) -> None:
        request = claim("prepare", {})
        self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        manager = self.manager()
        manager.verified = True
        manager.verified_slot_ids = frozenset({"slot-00"})
        task_root = self.slot.path / request.task_id
        external = Path(self.temporary.name) / "outside"
        external.mkdir()
        task_root.symlink_to(external, target_is_directory=True)
        with patch.object(manager, "_verify_local_role_path"):
            with self.assertRaises(BrokerError):
                manager.resolve_task_root(request.task_id)
        self.assertEqual(
            self.registry.task_slot(request.task_id, allow_quarantined=True)["state"],
            "quarantined",
        )

        another = claim(
            "prepare", {}, task_id="2" * 32, project_id="3" * 32, snapshot_id="4" * 32,
        )
        other_config = config(Path(self.temporary.name) / "other" / "broker")
        other_registry = BrokerRegistry(other_config)
        other_registry.initialize()
        other_registry.authorize(another, sign_claim(another, CLAIM_PRIVATE))
        other_manager = self.manager()
        other_manager.config = other_config
        other_manager.registry = other_registry
        other_manager.verified = True
        other_manager.verified_slot_ids = frozenset({"slot-00"})
        with patch.object(
            quota_slots_module, "_project_attributes", return_value=(42, 0),
        ), patch.object(
            other_manager, "_verify_observation",
        ), patch.object(
            other_manager, "_verify_local_role_path",
        ), self.assertRaises(BrokerError):
            other_manager.create_task_root(another.task_id)
        self.assertEqual(
            other_registry.task_slot(another.task_id, allow_quarantined=True)["state"],
            "quarantined",
        )

class DurableQuotaSlotRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.selected = config(Path(self.temporary.name) / "broker")
        self.registry = BrokerRegistry(self.selected)
        self.registry.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare_claim(self, index: int = 0) -> TaskClaim:
        return claim(
            "prepare", {},
            task_id=f"{index + 1:032x}",
            project_id=f"{index + 100:032x}",
            snapshot_id=f"{index + 200:032x}",
        )

    def test_concurrent_allocation_is_exclusive_and_pool_exhaustion_fails_closed(self) -> None:
        requests = (self.prepare_claim(0), self.prepare_claim(1))

        def allocate(request: TaskClaim) -> object:
            try:
                return self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
            except BrokerError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(allocate, requests))
        successes = [result for result in results if isinstance(result, str)]
        failures = [result for result in results if isinstance(result, BrokerError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "quota_slot_pool_exhausted")

    def test_failed_allocation_transaction_leaves_slot_and_task_unassigned(self) -> None:
        request = self.prepare_claim()
        prior = claim(
            "status", {}, task_id="0" * 32,
            project_id="1" * 32, snapshot_id="2" * 32,
        )
        with self.registry.connect() as connection:
            connection.execute(
                "INSERT INTO broker_tasks(task_id, project_id, snapshot_id, snapshot_digest, "
                "allocation_id, target_id, profile, image_digest, plan_digest, policy_decision_id, "
                "policy_version, approval_id, slot_id, slot_owner_token_hash, lease_generation, "
                "fencing_token_hash, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 'failed', ?, ?)",
                (
                    prior.task_id, prior.project_id, prior.snapshot_id, prior.snapshot_digest,
                    prior.allocation_id, prior.target_id, prior.profile, prior.image_digest,
                    prior.plan_digest, prior.policy_decision_id, prior.policy_version,
                    prior.approval_id, prior.lease_generation,
                    hashlib.sha256(prior.fencing_token.encode()).hexdigest(),
                    prior.issued_at, prior.issued_at,
                ),
            )
            connection.execute(
                "INSERT INTO broker_operations(operation_id, claim_id, replay_nonce, request_id, "
                "task_id, kind, state, request_digest, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "f" * 32, "1" * 32, "2" * 32, request.request_id,
                    prior.task_id, "status", "completed", "3" * 64, 1, 1,
                ),
            )
        with self.assertRaises(BrokerError) as duplicate:
            self.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.assertEqual(duplicate.exception.code, "operation_replayed")
        self.assertEqual(self.registry.slot_configurations()[0]["state"], "free")
        with self.assertRaises(BrokerError):
            self.registry.task(request.task_id)

    def test_restart_preserves_allocation_and_never_reuses_uncertain_slot(self) -> None:
        first = self.prepare_claim()
        self.registry.authorize(first, sign_claim(first, CLAIM_PRIVATE))
        restarted = BrokerRegistry(self.selected)
        restarted.initialize()
        self.assertEqual(restarted.task(first.task_id)["state"], "uncertain")
        self.assertEqual(
            restarted.task_slot(first.task_id, allow_quarantined=True)["state"],
            "quarantined",
        )
        second = self.prepare_claim(1)
        with self.assertRaises(BrokerError) as exhausted:
            restarted.authorize(second, sign_claim(second, CLAIM_PRIVATE))
        self.assertEqual(exhausted.exception.code, "quota_slot_pool_exhausted")

    def test_interrupted_container_cleanup_quarantines_task_and_slot(self) -> None:
        first = self.prepare_claim()
        self.registry.authorize(first, sign_claim(first, CLAIM_PRIVATE))
        self.registry.begin_operation(first.operation_id)
        self.registry.complete_operation(first.operation_id, "completed")
        self.registry.transition(first.task_id, "ready")
        self.registry.reserve_container(first.task_id, first.allocation_id)
        self.registry.bind_container(first.task_id, first.allocation_id, "a" * 64)
        self.registry.transition(first.task_id, "running")
        cleanup = replace(
            first,
            operation_id="1" * 32,
            tool_kind="remove",
            arguments_digest=hashlib.sha256(canonical_json({})).hexdigest(),
            claim_id="2" * 32, replay_nonce="3" * 32, request_id="4" * 32,
        )
        self.registry.authorize(cleanup, sign_claim(cleanup, CLAIM_PRIVATE))
        self.registry.begin_operation(cleanup.operation_id)
        self.registry.transition(first.task_id, "cancelling")
        self.registry.transition(first.task_id, "cleaning_up")
        restarted = BrokerRegistry(self.selected)
        restarted.initialize()
        self.assertEqual(restarted.task(first.task_id)["state"], "uncertain")
        self.assertEqual(
            restarted.task_slot(first.task_id, allow_quarantined=True)["state"],
            "quarantined",
        )


    def test_slot_is_reused_only_after_verified_release(self) -> None:
        first = self.prepare_claim()
        self.registry.authorize(first, sign_claim(first, CLAIM_PRIVATE))
        self.registry.begin_operation(first.operation_id)
        self.registry.complete_operation(first.operation_id, "completed")
        self.registry.transition(first.task_id, "ready")
        self.registry.transition(first.task_id, "cleaning_up")
        self.registry.transition(first.task_id, "removed")
        removal = replace(
            first,
            operation_id="c" * 32,
            tool_kind="remove",
            arguments_digest=hashlib.sha256(canonical_json({})).hexdigest(),
            claim_id="d" * 32, replay_nonce="e" * 32, request_id="f" * 32,
        )
        self.registry.authorize(removal, sign_claim(removal, CLAIM_PRIVATE))
        self.registry.begin_operation(removal.operation_id)
        evidence = "verified-stopped-and-removed"
        self.registry.mark_slot_release_pending(first.task_id, removal.operation_id, evidence)
        self.registry.complete_operation(removal.operation_id, "completed")
        self.registry.release_slot(first.task_id, evidence)
        second = self.prepare_claim(1)
        self.registry.authorize(second, sign_claim(second, CLAIM_PRIVATE))
        self.assertEqual(self.registry.task_slot(second.task_id)["slot_id"], "slot-00")

    def test_stale_owner_is_rejected_and_quarantined_slots_are_not_reused(self) -> None:
        first = self.prepare_claim()
        self.registry.authorize(first, sign_claim(first, CLAIM_PRIVATE))
        with self.registry.connect() as connection:
            connection.execute(
                "UPDATE broker_slots SET owner_token_hash = ? WHERE task_id = ?",
                ("0" * 64, first.task_id),
            )
        with self.assertRaises(BrokerError):
            self.registry.task_slot(first.task_id)
        self.registry.quarantine_slot(first.task_id)
        second = self.prepare_claim(1)
        with self.assertRaises(BrokerError) as unavailable:
            self.registry.authorize(second, sign_claim(second, CLAIM_PRIVATE))
        self.assertEqual(unavailable.exception.code, "quota_slot_pool_exhausted")

    def test_legacy_unassigned_task_records_are_reported_not_moved(self) -> None:
        first = self.prepare_claim()
        self.registry.authorize(first, sign_claim(first, CLAIM_PRIVATE))
        with self.registry.connect() as connection:
            connection.execute(
                "UPDATE broker_tasks SET slot_id = NULL, slot_owner_token_hash = NULL WHERE task_id = ?",
                (first.task_id,),
            )
        self.assertEqual(self.registry.unassigned_legacy_tasks(), (first.task_id,))
        with self.assertRaises(BrokerError):
            self.registry.task_slot(first.task_id)

    def test_legacy_registry_schema_migrates_without_reassigning_old_tasks(self) -> None:
        legacy = self.prepare_claim()
        legacy_config = config(Path(self.temporary.name) / "legacy" / "broker")
        legacy_registry = BrokerRegistry(legacy_config)
        legacy_config.state_dir.mkdir(mode=0o700, exist_ok=True)
        with sqlite3.connect(legacy_registry.path) as connection:
            connection.execute("""
                CREATE TABLE broker_tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    allocation_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    image_digest TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    policy_decision_id TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    approval_id TEXT NOT NULL,
                    lease_generation INTEGER NOT NULL,
                    fencing_token_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            connection.execute(
                "INSERT INTO broker_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    legacy.task_id, legacy.project_id, legacy.snapshot_id,
                    legacy.snapshot_digest, legacy.allocation_id, legacy.target_id,
                    legacy.profile, legacy.image_digest, legacy.plan_digest,
                    legacy.policy_decision_id, legacy.policy_version, legacy.approval_id,
                    legacy.lease_generation, hashlib.sha256(legacy.fencing_token.encode()).hexdigest(),
                    "ready", legacy.issued_at, legacy.issued_at,
                ),
            )
        os.chmod(legacy_registry.path, 0o600)
        legacy_registry.initialize()
        row = legacy_registry.task(legacy.task_id)
        self.assertIsNone(row["slot_id"])
        self.assertEqual(legacy_registry.unassigned_legacy_tasks(), (legacy.task_id,))

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
            registry.reserve_container(prepared.task_id, prepared.allocation_id)
            registry.bind_container(prepared.task_id, prepared.allocation_id, "e" * 64)
            registry.transition(prepared.task_id, "running")
            restarted = BrokerRegistry(registry.config)
            restarted.initialize()
            self.assertEqual(restarted.task(prepared.task_id)["state"], "uncertain")
            with self.assertRaises(BrokerError) as quarantined:
                restarted.owned_container(prepared.task_id)
            self.assertEqual(quarantined.exception.code, "container_quarantined")

    def test_restart_reports_durable_uncertainty_without_replaying_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broker_config = config(Path(directory) / "broker")
            registry = BrokerRegistry(broker_config)
            registry.initialize()
            request = claim("prepare", {})
            registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
            registry.begin_operation(request.operation_id)
            registry.initialize()
            recorded: list[tuple[str, str]] = []

            async def record_outcome(task: TaskClaim, signature: str, state: str) -> None:
                self.assertEqual(task.operation_id, request.operation_id)
                self.assertEqual(signature, sign_claim(request, CLAIM_PRIVATE))
                recorded.append((task.operation_id, state))

            service = BrokerService(
                broker_config, operation_outcome_recorder=record_outcome,
            )
            asyncio.run(service.recover())
            self.assertEqual(recorded, [(request.operation_id, "uncertain")])
            operation = registry.uncertain_operations()[0]
            self.assertEqual(operation["state"], "uncertain")
            self.assertEqual(registry.task(request.task_id)["state"], "uncertain")


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
            task_root = root / "slot" / ("1" * 32)
            task_root.parent.mkdir(mode=0o700, parents=True)
            task_root.mkdir(mode=0o700)
            workspace = stage_snapshot(task_root, self.manifest, {"src/module.py": self.content})
            self.assertEqual((workspace / "src/module.py").read_bytes(), self.content)
            (workspace / "src/module.py").write_bytes(b"changed\n")
            self.assertEqual((task_root / "snapshot" / "src/module.py").read_bytes(), self.content)
            staged = load_staged_manifest(task_root, self.digest)
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
                    claim_key=CLAIM_PUBLIC, core_ca_file=Path(directory) / "ca.pem",
                    broker_cert_file=Path(directory) / "broker.pem",
                    broker_key_file=Path(directory) / "broker.key",
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
                    claim_key=CLAIM_PUBLIC, core_ca_file=selected.core_ca_file,
                    broker_cert_file=selected.broker_cert_file,
                    broker_key_file=selected.broker_key_file,
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
        self.service.quota_manager = FakeQuotaManager(self.selected, self.service.registry)
        self.service.runtime.workspace_resolver = lambda task_id: self.service.task_paths(
            task_id, allow_quarantined=True,
        ).workspace
        self.service.registry.initialize()
        self.service.readiness = BrokerReadiness(
            True, True, {"workspace_quota": True}, (),
        )
        self.service._started = True

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_dispatch_is_blocked_when_execution_readiness_is_false(self) -> None:
        self.service.readiness = BrokerReadiness(
            False, False, {"workspace_quota": False}, ("storage_inspector",),
        )
        request = claim("prepare", {})
        with self.assertRaises(BrokerError) as unavailable:
            await self.service.dispatch(OperationRequest(
                schema_version=1, claim=request.to_dict(),
                claim_signature=sign_claim(request, CLAIM_PRIVATE),
                kind="prepare", arguments={},
            ))
        self.assertEqual(unavailable.exception.code, "execution_unavailable")

    async def test_startup_does_not_initialize_sqlite_before_state_role_verification(self) -> None:
        service = BrokerService(self.selected, snapshot_retriever=self.snapshots)
        with (
            patch.object(service.quota_manager, "verify_storage_role", return_value=False),
            patch.object(service.registry, "initialize") as initialize,
        ):
            await service.start()
        initialize.assert_not_called()
        self.assertFalse(service.readiness.execution_available)
        self.assertTrue(service._started)

    async def test_startup_does_not_probe_podman_when_storage_roles_are_unverified(self) -> None:
        service = BrokerService(self.selected, snapshot_retriever=self.snapshots)
        with (
            patch.object(service.quota_manager, "verify_storage_role", return_value=True),
            patch.object(
                service.quota_manager, "verify_startup",
                return_value=(False, ("storage_role_unavailable",)),
            ),
            patch.object(service.runtime, "verify_startup", new_callable=AsyncMock) as verify_runtime,
        ):
            await service.start()
        try:
            verify_runtime.assert_not_awaited()
            self.assertFalse(service.readiness.execution_available)
        finally:
            await service.close()

    async def test_storage_watchdog_detects_idle_loss_and_does_not_reenable_readiness(self) -> None:
        service = BrokerService(self.selected, snapshot_retriever=self.snapshots)
        service.config = replace(
            self.selected, storage_watchdog_interval_seconds=0.1,
        )
        service.quota_manager = FakeQuotaManager(self.selected, service.registry)
        service.runtime.workspace_resolver = lambda task_id: service.task_paths(
            task_id, allow_quarantined=True,
        ).workspace
        service.quota_manager.verify_startup = Mock(side_effect=[
            (True, ()), (True, ()), (False, ("storage_role_unavailable",)),
            (True, ()),
        ])
        with (
            patch.object(
                service.quota_manager, "verify_storage_role",
                return_value=True, create=True,
            ),
            patch.object(
                service.runtime, "verify_startup",
                new_callable=AsyncMock,
                return_value=BrokerReadiness(True, True, {"runtime": True}, ()),
            ),
        ):
            await service.start()
            self.assertIsNotNone(service._storage_watchdog_task)
            deadline = asyncio.get_running_loop().time() + 2
            while service.readiness.execution_available and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            self.assertFalse(service.readiness.execution_available)
            self.assertIn("storage_role_unavailable", service.readiness.reasons)
            await asyncio.sleep(0.15)
            self.assertFalse(service.readiness.execution_available)
            self.assertEqual(service.quota_manager.verify_startup.call_count, 4)
            await service.close()
        self.assertIsNone(service._storage_watchdog_task)

    async def test_storage_watchdog_unexpected_cancellation_revokes_readiness(self) -> None:
        service = BrokerService(self.selected, snapshot_retriever=self.snapshots)
        service.config = replace(
            self.selected, storage_watchdog_interval_seconds=0.1,
        )
        service.readiness = BrokerReadiness(True, True, {"workspace_quota": True}, ())
        service._started = True
        service._start_storage_watchdog()
        watchdog = service._storage_watchdog_task
        self.assertIsNotNone(watchdog)
        watchdog.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await watchdog
        await asyncio.sleep(0)
        self.assertFalse(service.readiness.execution_available)
        self.assertIn("storage_watchdog_stopped", service.readiness.reasons)
        self.assertIsNotNone(service._storage_watchdog_task)
        self.assertIsNot(service._storage_watchdog_task, watchdog)
        await service.close()

    async def test_storage_watchdog_is_singleton_and_shutdown_is_deterministic(self) -> None:
        service = BrokerService(self.selected, snapshot_retriever=self.snapshots)
        service.config = replace(
            self.selected, storage_watchdog_interval_seconds=0.1,
        )
        service._started = True
        service._start_storage_watchdog()
        watchdog = service._storage_watchdog_task
        service._start_storage_watchdog()
        self.assertIs(service._storage_watchdog_task, watchdog)
        await service.close()
        self.assertTrue(watchdog.cancelled())

    async def test_storage_assurance_loss_quarantines_and_verifies_exact_container(self) -> None:
        request = claim("prepare", {})
        self.service.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.service.registry.transition(request.task_id, "ready")
        self.service.registry.reserve_container(request.task_id, request.allocation_id)
        self.service.registry.bind_container(request.task_id, request.allocation_id, "c" * 64)
        self.service.runtime.contain_recorded = AsyncMock(
            return_value=runtime_module.ContainmentResult(
                "verified_stopped", evidence="f" * 64,
            ),
        )

        await self.service._handle_storage_assurance_loss(("storage_role_unavailable",))

        self.service.runtime.contain_recorded.assert_awaited_once_with(request.task_id)
        container = self.service.registry.container_record(request.task_id)
        self.assertEqual(container["lifecycle_state"], "terminated")
        self.assertEqual(
            self.service.registry.task_slot(request.task_id, allow_quarantined=True)["state"],
            "quarantined",
        )
        self.assertFalse(self.service.readiness.execution_available)

    async def test_storage_loss_with_unconfirmed_termination_stays_quarantined(self) -> None:
        request = claim("prepare", {})
        self.service.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.service.registry.transition(request.task_id, "ready")
        self.service.registry.reserve_container(request.task_id, request.allocation_id)
        self.service.registry.bind_container(request.task_id, request.allocation_id, "d" * 64)
        self.service.runtime.contain_recorded = AsyncMock(
            return_value=runtime_module.ContainmentResult(
                "still_running", error_code="forced_stop_unconfirmed",
            ),
        )

        await self.service._handle_storage_assurance_loss(("storage_role_stale",))

        container = self.service.registry.container_record(request.task_id)
        self.assertEqual(container["lifecycle_state"], "quarantined")
        self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)
        self.assertFalse(self.service.readiness.execution_available)

    async def test_storage_loss_containment_is_independent_of_quota_readiness(self) -> None:
        request = claim("prepare", {})
        self.service.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.service.registry.transition(request.task_id, "ready")
        self.service.registry.reserve_container(request.task_id, request.allocation_id)
        self.service.registry.bind_container(request.task_id, request.allocation_id, "e" * 64)
        self.service.quota_manager.runtime_containment_available = False
        self.service.runtime.contain_recorded = AsyncMock(
            return_value=runtime_module.ContainmentResult(
                "verified_stopped", evidence="a" * 64,
            ),
        )
        await self.service._handle_storage_assurance_loss(("storage_role_unavailable",))
        self.service.runtime.contain_recorded.assert_awaited_once_with(request.task_id)
        self.assertEqual(
            self.service.registry.container_record(request.task_id)["lifecycle_state"],
            "terminated",
        )
        self.assertNotIn("storage_containment_unconfirmed", self.service.readiness.reasons)

    def _register_owned_container(self, container_id: str = "b" * 64) -> Any:
        self.service.runtime.image_id = "image-id"
        request = claim("prepare", {})
        self.service.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.service.registry.transition(request.task_id, "ready")
        self.service.registry.reserve_container(
            request.task_id, request.allocation_id,
        )
        self.service.registry.bind_container(
            request.task_id, request.allocation_id, container_id,
        )
        return self.service.registry.container_record(request.task_id)

    @staticmethod
    def _containment_info(record: Any, *, running: bool, labels: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "Id": record["container_id"],
            "Image": "image-id",
            "Config": {"Labels": labels or {
                "synai.broker.task": record["task_id"],
                "synai.broker.allocation": record["allocation_id"],
                "synai.broker.image-digest": record["image_digest"],
                "synai.broker.snapshot-digest": record["snapshot_digest"],
            }},
            "State": {"Running": running, "Status": "running" if running else "exited"},
        }

    async def test_containment_graceful_stop_is_independently_verified(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        runtime.inspect_recorded = AsyncMock(side_effect=[
            self._containment_info(record, running=True),
            self._containment_info(record, running=False),
        ])
        runtime._run = AsyncMock(return_value="")
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "verified_stopped")
        self.assertIsNotNone(result.evidence)
        runtime._run.assert_awaited_once_with(
            "stop", "--time", "3", record["container_id"],
            timeout=5, maximum=4096,
        )

    async def test_containment_escalates_after_graceful_timeout_and_verifies_kill(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        runtime.inspect_recorded = AsyncMock(side_effect=[
            self._containment_info(record, running=True),
            self._containment_info(record, running=True),
            self._containment_info(record, running=False),
        ])
        runtime._run = AsyncMock(side_effect=[TimeoutError(), ""])
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "verified_stopped")
        self.assertEqual(runtime._run.await_count, 2)
        self.assertEqual(runtime._run.await_args_list[1].args, ("kill", record["container_id"]))

    async def test_containment_reports_still_running_after_bounded_force_attempt(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        runtime.inspect_recorded = AsyncMock(side_effect=[
            self._containment_info(record, running=True),
            self._containment_info(record, running=True),
            self._containment_info(record, running=True),
        ])
        runtime._run = AsyncMock(side_effect=[
            BrokerError("runtime_operation_failed", "stop failed", 502),
            BrokerError("runtime_operation_failed", "kill failed", 502),
        ])
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "still_running")
        self.assertEqual(runtime._run.await_count, 2)

    async def test_containment_accepts_already_stopped_without_termination_command(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        runtime.inspect_recorded = AsyncMock(
            return_value=self._containment_info(record, running=False),
        )
        runtime._run = AsyncMock()
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "verified_stopped")
        runtime._run.assert_not_awaited()

    async def test_containment_reports_absent_only_after_exact_exists_check(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        runtime.inspect_recorded = AsyncMock(side_effect=BrokerError(
            "runtime_operation_failed", "inspect error", 502,
        ))
        runtime._container_exists = AsyncMock(return_value=False)
        runtime._run = AsyncMock()
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "verified_absent")
        runtime._container_exists.assert_awaited_once_with(record["container_id"])
        runtime._run.assert_not_awaited()

    async def test_containment_runtime_unavailable_is_not_absence(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        runtime.inspect_recorded = AsyncMock(side_effect=BrokerError(
            "runtime_operation_failed", "runtime unavailable", 503,
        ))
        runtime._container_exists = AsyncMock(side_effect=BrokerError(
            "runtime_unavailable", "runtime unavailable", 503,
        ))
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "runtime_unavailable")

    async def test_containment_identity_mismatch_never_issues_runtime_command(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        runtime.inspect_recorded = AsyncMock(return_value=self._containment_info(
            record, running=True,
            labels={"synai.broker.task": "9" * 32},
        ))
        runtime._run = AsyncMock()
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "identity_mismatch")
        runtime._run.assert_not_awaited()

    async def test_containment_image_identity_mismatch_never_issues_runtime_command(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        info = self._containment_info(record, running=True)
        info["Image"] = "unexpected-image-id"
        runtime.inspect_recorded = AsyncMock(return_value=info)
        runtime._run = AsyncMock()
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "identity_mismatch")
        runtime._run.assert_not_awaited()

    async def test_containment_container_id_mismatch_never_issues_runtime_command(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        info = self._containment_info(record, running=True)
        info["Id"] = "c" * 64
        runtime.inspect_recorded = AsyncMock(return_value=info)
        runtime._run = AsyncMock()
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "identity_mismatch")
        runtime._run.assert_not_awaited()

    async def test_containment_rejects_partial_container_id(self) -> None:
        record = self._register_owned_container(container_id="b" * 12)
        runtime = self.service.runtime
        runtime._run = AsyncMock()
        runtime.emergency_containment_available = Mock(return_value=True)

        result = await runtime.contain_recorded(record["task_id"])

        self.assertEqual(result.state, "identity_mismatch")
        runtime._run.assert_not_awaited()

    async def test_containment_serializes_with_ordinary_task_cancellation(self) -> None:
        record = self._register_owned_container()
        runtime = self.service.runtime
        entered = asyncio.Event()
        release = asyncio.Event()

        async def inspect(_: str) -> dict[str, Any]:
            entered.set()
            await release.wait()
            return self._containment_info(record, running=False)

        runtime.inspect_recorded = AsyncMock(side_effect=inspect)
        runtime._terminate_container = AsyncMock(return_value="terminated")
        runtime.emergency_containment_available = Mock(return_value=True)

        containment = asyncio.create_task(runtime.contain_recorded(record["task_id"]))
        await entered.wait()
        cancellation = asyncio.create_task(runtime.terminate_owned(record["task_id"]))
        await asyncio.sleep(0)
        runtime._terminate_container.assert_not_awaited()
        release.set()

        result, _ = await asyncio.gather(containment, cancellation)

        self.assertEqual(result.state, "verified_stopped")
        runtime._terminate_container.assert_awaited_once_with(
            record["task_id"], recovery=False,
        )

    async def test_containment_missing_registry_record_is_unresolved(self) -> None:
        runtime = self.service.runtime
        runtime.emergency_containment_available = Mock(return_value=True)
        with patch.object(self.service.registry, "container_record", return_value=None):
            result = await runtime.contain_recorded("1" * 32)
        self.assertEqual(result.state, "identity_mismatch")

    async def test_assurance_loss_containment_does_not_require_quota_inspector(self) -> None:
        record = self._register_owned_container()
        self.service.quota_manager.runtime_containment_available = False
        self.service.runtime.contain_recorded = AsyncMock(
            return_value=runtime_module.ContainmentResult(
                "verified_stopped", evidence="e" * 64,
            ),
        )

        await self.service._handle_storage_assurance_loss(("inspector_unavailable",))

        self.service.runtime.contain_recorded.assert_awaited_once_with(record["task_id"])
        self.assertEqual(
            self.service.registry.container_record(record["task_id"])["lifecycle_state"],
            "terminated",
        )
        self.assertFalse(self.service.readiness.execution_available)

    async def test_concurrent_storage_loss_notifications_share_one_containment_attempt(self) -> None:
        record = self._register_owned_container()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def contain(_: str) -> runtime_module.ContainmentResult:
            entered.set()
            await release.wait()
            return runtime_module.ContainmentResult("verified_stopped", evidence="1" * 64)

        self.service.runtime.contain_recorded = AsyncMock(side_effect=contain)
        first = asyncio.create_task(
            self.service._handle_storage_assurance_loss(("inspector_unavailable",)),
        )
        await entered.wait()
        second = asyncio.create_task(
            self.service._handle_storage_assurance_loss(("inspector_unavailable",)),
        )
        await asyncio.sleep(0)
        self.assertEqual(self.service.runtime.contain_recorded.await_count, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.service.runtime.contain_recorded.await_count, 1)
        self.assertEqual(
            self.service.registry.container_record(record["task_id"])["lifecycle_state"],
            "terminated",
        )

    async def test_cancelling_loss_handler_does_not_cancel_containment_or_skip_evidence(self) -> None:
        record = self._register_owned_container()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def contain(_: str) -> runtime_module.ContainmentResult:
            entered.set()
            await release.wait()
            return runtime_module.ContainmentResult("verified_stopped", evidence="2" * 64)

        self.service.runtime.contain_recorded = AsyncMock(side_effect=contain)
        handler = asyncio.create_task(
            self.service._handle_storage_assurance_loss(("inspector_unavailable",)),
        )
        await entered.wait()
        handler.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handler
        containment = self.service._containment_tasks[record["task_id"]]
        self.assertFalse(containment.cancelled())
        release.set()
        result = await containment
        self.assertEqual(result.state, "verified_stopped")
        self.assertEqual(
            self.service.registry.container_record(record["task_id"])["lifecycle_state"],
            "terminated",
        )
        self.assertFalse(self.service.readiness.execution_available)

    async def test_failed_quarantine_persistence_does_not_claim_containment_durable(self) -> None:
        record = self._register_owned_container()
        self.service.runtime.contain_recorded = AsyncMock(
            return_value=runtime_module.ContainmentResult("verified_stopped", evidence="3" * 64),
        )
        with patch.object(
            self.service.registry, "quarantine_all_for_storage_loss",
            side_effect=sqlite3.OperationalError("state write failed"),
        ):
            await self.service._handle_storage_assurance_loss(("inspector_unavailable",))
        self.service.runtime.contain_recorded.assert_awaited_once_with(record["task_id"])
        self.assertEqual(
            self.service.registry.container_record(record["task_id"])["lifecycle_state"],
            "owned",
        )
        self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)

    async def test_unreadable_registry_never_claims_containment_confirmed(self) -> None:
        self.service.registry.container_records = Mock(
            side_effect=sqlite3.OperationalError("registry unavailable"),
        )
        self.service.registry.quarantine_all_for_storage_loss = Mock(
            side_effect=sqlite3.OperationalError("registry unavailable"),
        )
        self.service.runtime.contain_recorded = AsyncMock()

        await self.service._handle_storage_assurance_loss(("inspector_unavailable",))

        self.service.runtime.contain_recorded.assert_not_awaited()
        self.assertIn("storage_containment_unconfirmed", self.service.readiness.reasons)
        self.assertFalse(self.service.readiness.execution_available)

    async def test_unresolved_containment_survives_registry_restart_as_quarantined(self) -> None:
        record = self._register_owned_container()
        self.service.runtime.contain_recorded = AsyncMock(
            return_value=runtime_module.ContainmentResult(
                "runtime_unavailable", error_code="runtime_unavailable",
            ),
        )
        await self.service._handle_storage_assurance_loss(("inspector_unavailable",))

        restarted = BrokerRegistry(self.selected)
        restarted.initialize()
        persisted = restarted.container_record(record["task_id"])
        self.assertEqual(persisted["lifecycle_state"], "quarantined")
        self.assertIn("runtime_unavailable", persisted["reconciliation_evidence"])
        self.assertFalse(self.service.readiness.execution_available)

    async def test_container_tmpfs_has_both_byte_and_inode_limits(self) -> None:
        runtime = RootlessPodmanRuntime(self.selected, self.service.registry)
        runtime.readiness = BrokerReadiness(True, True, {}, ())
        request = claim("prepare", {})
        self.service.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.service.registry.transition(request.task_id, "ready")
        with patch.object(runtime, "_run", new_callable=AsyncMock) as run:
            run.return_value = "a" * 64
            await runtime.create_task(
                request.task_id, request.allocation_id,
                self.selected.quota_slots[0].path / request.task_id / "workspace",
            )

        arguments = run.await_args.args
        tmpfs = arguments[arguments.index("--tmpfs") + 1]
        self.assertEqual(
            tmpfs,
            "/tmp:rw,noexec,nosuid,nodev,size=64m,nr_inodes=4096,mode=1777",
        )

    async def test_runtime_startup_replaces_probe_placeholders_with_live_results(self) -> None:
        runtime = RootlessPodmanRuntime(self.selected, self.service.registry)
        checks = {
            "podman_available": True,
            "rootless_podman": False,
            "cgroup_v2": False,
            "seccomp_profile": False,
            "linux_security_module": False,
            "user_namespace": False,
            "approved_image_present": False,
            "storage_layout": False,
            "workspace_quota": True,
        }
        base = BrokerReadiness(
            False, False, checks,
            tuple(name for name, passed in checks.items() if not passed),
        )
        info = json.dumps({
            "host": {
                "security": {
                    "rootless": True, "seccompEnabled": True,
                },
                "cgroupVersion": "v2",
                "apparmorEnabled": True,
                "selinuxEnabled": False,
            },
            "store": {
                "graphRoot": str(next(
                    role.path for role in self.selected.storage_roles
                    if role.kind == "podman_graph"
                )),
                "runRoot": str(next(
                    role.path for role in self.selected.storage_roles
                    if role.kind == "podman_runtime"
                )),
                "imageCopyTmpDir": str(next(
                    role.path for role in self.selected.storage_roles
                    if role.kind == "image_scratch"
                )),
            },
        })

        async def run(*args: str, **_: object) -> str:
            if args[0] == "info":
                return info
            if args[0] == "unshare":
                return "0 1000 1\n1 100000 65536\n"
            return f"{self.selected.image_digest} image-id"

        with (
            patch.object(runtime, "_run", side_effect=run),
            patch.object(runtime_module.Path, "is_file", return_value=True),
            patch.object(runtime_module.Path, "exists", return_value=True),
        ):
            readiness = await runtime.verify_startup(base)
        self.assertTrue(readiness.execution_available, (readiness.reasons, readiness.checks))
        self.assertTrue(readiness.checks["storage_layout"])

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
            claim_id="1" * 32, replay_nonce="2" * 32, request_id="3" * 32,
        )
        response = await self.service.dispatch(OperationRequest(
            schema_version=1, claim=read.to_dict(),
            claim_signature=sign_claim(read, CLAIM_PRIVATE),
            kind="read_file", arguments=read_args,
        ))
        self.assertEqual(response["result"]["content"], self.snapshots.content.decode())
        self.assertEqual(response["result"]["sha256"], hashlib.sha256(self.snapshots.content).hexdigest())

    async def test_interrupted_snapshot_staging_quarantines_its_durable_slot(self) -> None:
        original = claim("prepare", {})
        digest = self.snapshots.prepare_manifest(original.snapshot_id)
        request = replace(original, snapshot_digest=digest)

        def interrupt(task_root: Path, *_: object) -> Path:
            partial = task_root / "snapshot"
            partial.mkdir()
            (partial / "partial").write_text("incomplete")
            raise OSError("simulated staging interruption")

        payload = OperationRequest(
            schema_version=1, claim=request.to_dict(),
            claim_signature=sign_claim(request, CLAIM_PRIVATE),
            kind="prepare", arguments={},
        )
        with patch("synai.broker.app.stage_snapshot", side_effect=interrupt):
            with self.assertRaises(OSError):
                await self.service.dispatch(payload)
        self.assertEqual(self.service.registry.task(request.task_id)["state"], "uncertain")
        restarted = BrokerRegistry(self.selected)
        restarted.initialize()
        self.assertEqual(
            restarted.task_slot(request.task_id, allow_quarantined=True)["state"],
            "quarantined",
        )

    async def test_collected_artifacts_are_persisted_inside_the_assigned_slot(self) -> None:
        initial = claim("prepare", {})
        digest = self.snapshots.prepare_manifest(initial.snapshot_id)
        prepared = replace(initial, snapshot_digest=digest)
        await self.service.dispatch(OperationRequest(
            schema_version=1, claim=prepared.to_dict(),
            claim_signature=sign_claim(prepared, CLAIM_PRIVATE),
            kind="prepare", arguments={},
        ))
        updated_content = "updated\n"
        write_args = {
            "path": "src/file.txt",
            "content": updated_content,
            "expected_sha256": hashlib.sha256(self.snapshots.content).hexdigest(),
        }
        write = replace(
            prepared,
            operation_id="a" * 32,
            tool_kind="write_file",
            arguments_digest=hashlib.sha256(canonical_json(write_args)).hexdigest(),
            claim_id="1" * 32, replay_nonce="2" * 32, request_id="3" * 32,
        )
        await self.service.dispatch(OperationRequest(
            schema_version=1, claim=write.to_dict(),
            claim_signature=sign_claim(write, CLAIM_PRIVATE),
            kind="write_file", arguments=write_args,
        ))
        collect = replace(
            prepared,
            operation_id="b" * 32,
            tool_kind="collect",
            arguments_digest=hashlib.sha256(canonical_json({})).hexdigest(),
            claim_id="4" * 32, replay_nonce="5" * 32, request_id="6" * 32,
        )
        response = await self.service.dispatch(OperationRequest(
            schema_version=1, claim=collect.to_dict(),
            claim_signature=sign_claim(collect, CLAIM_PRIVATE),
            kind="collect", arguments={},
        ))
        artifact_path = (
            self.selected.quota_slots[0].path / prepared.task_id
            / "artifacts" / f"{collect.operation_id}.json"
        )
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(
            json.loads(artifact_path.read_text())[0]["artifact_base64"],
            response["result"]["changed_files"][0]["artifact_base64"],
        )
        with self.service.registry.connect() as connection:
            audit = connection.execute(
                "SELECT result_json FROM broker_operations WHERE operation_id = ?",
                (collect.operation_id,),
            ).fetchone()["result_json"]
        self.assertEqual(json.loads(audit)["artifact_manifest_path"], f"artifacts/{collect.operation_id}.json")
        self.assertNotIn("artifact_base64", audit)

    async def test_failed_snapshot_retrieval_leaves_task_cleanable(self) -> None:
        class MissingSnapshot:
            async def fetch_exact(self, **_: object) -> tuple[dict[str, object], dict[str, bytes]]:
                raise BrokerError("snapshot_not_found", "Exact snapshot unavailable.", 404)

        service = BrokerService(
            self.selected, snapshot_retriever=MissingSnapshot(), lease_validator=_valid_lease,
        )
        service.quota_manager = FakeQuotaManager(self.selected, service.registry)
        service.runtime.workspace_resolver = lambda task_id: service.task_paths(
            task_id, allow_quarantined=True,
        ).workspace
        service.registry.initialize()
        service.readiness = BrokerReadiness(True, True, {"workspace_quota": True}, ())
        service._started = True
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
            claim_id="4" * 32, replay_nonce="5" * 32, request_id="6" * 32,
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
            claim_id="7" * 32, replay_nonce="8" * 32, request_id="9" * 32,
        )
        result = await self.service.dispatch(OperationRequest(
            schema_version=1, claim=cancel.to_dict(),
            claim_signature=sign_claim(cancel, CLAIM_PRIVATE),
            kind="cancel", arguments={},
        ))
        self.assertEqual(result["result"]["state"], "removed")
        self.assertFalse((self.selected.quota_slots[0].path / prepared.task_id).exists())
        self.assertEqual(other.read_text(), "preserve")
        self.assertEqual(self.service.registry.task(prepared.task_id)["state"], "removed")

    async def test_missing_live_lease_authority_fails_closed(self) -> None:
        service = BrokerService(self.selected, snapshot_retriever=self.snapshots)
        service.registry.initialize()
        service._started = True
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


class BrokerMtlsBoundaryTests(unittest.TestCase):
    def test_cleartext_service_requests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = config(Path(directory) / "broker")
            service = BrokerService(selected)
            service.registry.initialize()
            app = create_app(service)
            client = TestClient(app)
            response = client.get("/v1/ready")
            self.assertEqual(response.status_code, 426)
            self.assertEqual(response.json()["error"]["code"], "mtls_required")

    def test_unsupported_request_version_is_rejected_over_tls_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = config(Path(directory) / "broker")
            service = BrokerService(selected)
            service.registry.initialize()
            client = TestClient(create_app(service), base_url="https://testserver")
            body = json.dumps({
                "schema_version": 2, "claim": {}, "claim_signature": "0" * 128,
                "kind": "prepare", "arguments": {},
            }, separators=(",", ":")).encode()
            response = client.post(
                "/v1/tasks/" + "1" * 32 + "/operations",
                content=body, headers={"content-type": "application/json"},
            )
            self.assertEqual(response.status_code, 422)


async def _valid_lease(_: TaskClaim, __: str) -> bool:
    return True


if __name__ == "__main__":
    unittest.main()
