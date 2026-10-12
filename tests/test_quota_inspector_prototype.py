from __future__ import annotations

import asyncio
import json
import os
import selectors
import socket
import stat
import struct
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from synai.broker.config import StorageRole
from synai.broker.protocol import BrokerError
from synai.broker.quota_slots import QuotaSlotManager
from synai.broker.storage_inspector import (
    InspectorInconsistent,
    InspectorInvalid,
    InspectorStale,
    InspectorUnavailable,
    MAX_FRAME_BYTES,
    StorageInspectionRequest,
    UnixStorageInspectorClient,
    _reject_json_constant,
    _unique_json_object,
    inventory_generation,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/quota_inspector_v1"
BINARY = ROOT / "prototypes/quota-inspector/target/debug/fixture-harness"


def fixture_document(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def fixture_roles() -> tuple[StorageRole, ...]:
    return tuple(StorageRole.from_dict(role) for role in fixture_document("inventory.json")["roles"])


def client_for(path: Path | None = None) -> UnixStorageInspectorClient:
    return UnixStorageInspectorClient(
        path, fixture_roles(), inspector_uid=os.geteuid(), inspector_gid=os.getegid(),
        timeout_seconds=0.8, max_age_seconds=5,
        inventory_generation=inventory_generation(fixture_roles()),
    )


def mutate(template: dict, case: dict) -> dict:
    value = {**template, **case.get("changes", {})}
    if "remove" in case:
        del value[case["remove"]]
    return value


def receive_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise EOFError("Fixture connection closed")
        result.extend(chunk)
    return bytes(result)


class GoldenProtocolTests(unittest.TestCase):
    def test_synthetic_inventory_uses_authoritative_canonical_generation(self):
        document = fixture_document("inventory.json")
        self.assertEqual(document["label"], "synthetic_offline_only")
        for role in fixture_roles():
            role.validate()
        self.assertEqual(inventory_generation(fixture_roles()), document["expected_generation"])
        self.assertEqual(
            inventory_generation(tuple(reversed(fixture_roles()))),
            document["expected_generation"],
        )

    def test_shared_valid_and_invalid_request_golden_vectors(self):
        vectors = fixture_document("exchanges.json")
        request = vectors["request"]
        options = {
            "approved_roles": frozenset(role.role_id for role in fixture_roles()),
            "expected_generation": inventory_generation(fixture_roles()),
        }
        self.assertEqual(StorageInspectionRequest.parse(request, **options).to_wire(), request)
        for case in vectors["invalid_requests"]:
            with self.subTest(case=case["name"]), self.assertRaises(InspectorInvalid):
                StorageInspectionRequest.parse(mutate(request, case), **options)

    def test_shared_success_error_and_invalid_response_vectors(self):
        vectors = fixture_document("exchanges.json")
        request = vectors["request"]
        with patch("synai.broker.storage_inspector.time.time", return_value=1700000000):
            result = client_for()._parse_response(
                vectors["response"], request["role_id"], request["challenge"],
            )
            self.assertEqual(result.mount_id, 42)
            with self.assertRaises(InspectorUnavailable):
                client_for()._parse_response(
                    vectors["unavailable"], request["role_id"], request["challenge"],
                )
            errors = {
                "invalid": InspectorInvalid, "stale": InspectorStale,
                "inconsistent": InspectorInconsistent,
            }
            for case in vectors["invalid_responses"]:
                with self.subTest(case=case["name"]), self.assertRaises(errors[case["error"]]):
                    client_for()._parse_response(
                        mutate(vectors["response"], case),
                        request["role_id"], request["challenge"],
                    )

    def test_changed_epoch_and_old_challenge_are_refused(self):
        vectors = fixture_document("exchanges.json")
        response = vectors["response"]
        client = client_for()
        with patch("synai.broker.storage_inspector.time.time", return_value=1700000000):
            client._parse_response(response, "fixture-role", response["challenge"])
            with self.assertRaises(InspectorStale):
                client._parse_response(
                    {**response, "helper_boot_epoch": "b" * 32},
                    "fixture-role", response["challenge"],
                )
            with self.assertRaises(InspectorInvalid):
                client._parse_response(response, "fixture-role", "c" * 64)

    def test_python_json_decoder_refuses_duplicate_and_nonfinite_values(self):
        for payload in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'"\xff"'):
            with self.subTest(payload=payload), self.assertRaises((ValueError, UnicodeError)):
                json.loads(
                    payload, object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )

    def test_success_does_not_authorize_real_local_filesystem(self):
        from tests.test_broker import config

        with tempfile.TemporaryDirectory() as directory:
            selected = replace(config(Path(directory) / "broker"), storage_roles=fixture_roles())
            manager = QuotaSlotManager(selected, object())
            response = fixture_document("exchanges.json")["response"]
            with patch("synai.broker.storage_inspector.time.time", return_value=1700000000):
                observation = client_for()._parse_response(
                    response, "fixture-role", response["challenge"],
                )
            # No mock replaces the filesystem authority checks.
            with self.assertRaises((OSError, BrokerError, InspectorInvalid)):
                manager._verify_local_role_path(fixture_roles()[0], observation)
            self.assertFalse(manager.verified)
            self.assertFalse(manager.runtime_containment_available)
            self.assertEqual(manager.verified_slot_ids, frozenset())


class FixtureHarness:
    def __init__(
        self, parent: Path, *, lifetime_ms: int = 1600,
        backend: str = "fixture", allowed_uid: int | None = None,
        allowed_gid: int | None = None, inventory: Path | None = None,
    ):
        self.process = subprocess.Popen(
            [
                str(BINARY), "--parent", str(parent),
                "--inventory", str(inventory or FIXTURES / "inventory.json"),
                "--allowed-uid", str(os.geteuid() if allowed_uid is None else allowed_uid),
                "--allowed-gid", str(os.getegid() if allowed_gid is None else allowed_gid),
                "--lifetime-ms", str(lifetime_ms), "--backend", backend,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            assert self.process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=5):
                    raise AssertionError("Rust fixture harness startup timed out")
            line = self.process.stdout.readline(4097)
            if not line or len(line) > 4096:
                raise AssertionError("Rust fixture harness returned no bounded startup frame")
            self.startup = json.loads(line)
            self.path = Path(self.startup["socket_path"])
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            self.process.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=2)
            raise AssertionError("Rust fixture harness failed its bounded lifetime")

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(1)
        try:
            connection.connect(str(self.path))
        except BaseException:
            connection.close()
            raise
        return connection

    def exchange(self, payload: bytes, *, fragmented: bool = False) -> dict:
        frame = struct.pack("!I", len(payload)) + payload
        with self.connect() as connection:
            if fragmented:
                for offset in range(0, len(frame), 7):
                    connection.sendall(frame[offset:offset + 7])
            else:
                connection.sendall(frame)
            size = struct.unpack("!I", receive_exact(connection, 4))[0]
            if not 1 <= size <= MAX_FRAME_BYTES:
                raise AssertionError("Rust fixture server emitted an oversized frame")
            return json.loads(receive_exact(connection, size))


@unittest.skipUnless(
    BINARY.is_file() and hasattr(socket, "SO_PEERCRED") and os.geteuid() != 0,
    "Build the offline fixture-harness with cargo first; ordinary Linux user required",
)
class RustUnixCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synai-offline-inspector-")
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.parent.chmod(0o700)

    def start(self, **options) -> FixtureHarness:
        harness = FixtureHarness(self.parent, **options)
        self.addCleanup(harness.close)
        return harness

    def request(self, challenge: str | None = None) -> dict:
        request = StorageInspectionRequest.issue(
            "fixture-role", inventory_generation(fixture_roles()),
        ).to_wire()
        if challenge is not None:
            request["challenge"] = challenge
        return request

    def test_actual_python_client_observes_fixture_and_peer_credentials(self):
        harness = self.start()
        self.assertTrue(harness.startup["fixture_only"])
        self.assertEqual(stat.S_IMODE(harness.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(harness.path.stat().st_mode), 0o600)
        with harness.connect() as connection:
            pid, uid, gid = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
            )
            self.assertEqual((pid, uid, gid), (harness.process.pid, os.geteuid(), os.getegid()))
        result = client_for(harness.path).inspect("fixture-role")
        self.assertEqual(result.bytes_used, 64)
        self.assertEqual(result.project_id, 4242)
        self.assertEqual(result.owner_uid, 4242)
        self.assertEqual(result.helper_boot_epoch, harness.startup["helper_boot_epoch"])

    def test_shared_golden_request_and_fragmented_frame(self):
        harness = self.start()
        vectors = fixture_document("exchanges.json")
        response = harness.exchange(json.dumps(vectors["request"]).encode(), fragmented=True)
        expected = {
            **vectors["response"], "observed_at": response["observed_at"],
            "helper_boot_epoch": harness.startup["helper_boot_epoch"],
        }
        self.assertEqual(response, expected)
        self.assertLessEqual(abs(int(time.time()) - response["observed_at"]), 2)

    def test_generation_matches_python_sorted_roles_and_unicode_escaping(self):
        document = fixture_document("inventory.json")
        mount = "/synthetic/caf\u00e9-\U0001f642"
        first = {**document["roles"][0], "mount_path": mount, "path": mount + "/first"}
        second = {
            **first, "role_id": "fixture-other", "path": mount + "/second",
            "project_id": 4243,
        }
        document["roles"] = [first, second]
        document["observations"] = {
            role["role_id"]: {
                **document["observations"]["fixture-role"],
                "mount_target": mount, "project_id": role["project_id"],
            }
            for role in document["roles"]
        }
        generation = inventory_generation(tuple(StorageRole.from_dict(role) for role in document["roles"]))
        document["expected_generation"] = generation
        inventory = self.parent / "synthetic-inventory.json"
        inventory.write_text(json.dumps(document, ensure_ascii=False))
        harness = self.start(inventory=inventory)
        self.assertEqual(harness.startup["inventory_generation"], generation)
        request = self.request()
        request["inventory_generation"] = generation
        self.assertEqual(harness.exchange(json.dumps(request).encode())["mount_target"], mount)

    def test_invalid_inventory_is_refused_before_socket_creation(self):
        cases = (
            {"name": "unknown-field", "changes": {"command": "host"}},
            {"name": "missing-label", "remove": "label"},
            {"name": "wrong-label", "changes": {"label": "production"}},
            {"name": "wrong-generation", "changes": {"expected_generation": "0" * 64}},
            {"name": "duplicate-role", "duplicate": True},
            {"name": "bad-usage", "observation": {"bytes_used": 1048577}},
            {"name": "bad-counter-type", "observation": {"bytes_used": True}},
            {"name": "counter-overflow", "observation": {"bytes_used": 2**64}},
            {"name": "wrong-quota", "observation": {"byte_limit": 1024}},
            {"name": "disabled-quota", "observation": {"quota_configuration_enabled": False}},
            {"name": "not-inherited", "observation": {"inheritance_enabled": False}},
        )
        for case in cases:
            document = mutate(fixture_document("inventory.json"), case)
            if case.get("duplicate"):
                document["roles"].append(dict(document["roles"][0]))
            document["observations"]["fixture-role"].update(case.get("observation", {}))
            inventory = self.parent / "bad-inventory.json"
            inventory.write_text(json.dumps(document))
            with self.subTest(case=case["name"]):
                result = subprocess.run(
                    [
                        str(BINARY), "--parent", str(self.parent), "--inventory", str(inventory),
                        "--allowed-uid", str(os.geteuid()), "--allowed-gid", str(os.getegid()),
                        "--lifetime-ms", "50", "--backend", "fixture",
                    ],
                    capture_output=True, timeout=3,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertLess(len(result.stderr), 1024)
                self.assertEqual(list(self.parent.iterdir()), [inventory])

    def test_missing_peer_and_unbounded_lifetime_are_refused(self):
        base = [
            str(BINARY), "--parent", str(self.parent),
            "--inventory", str(FIXTURES / "inventory.json"),
        ]
        for arguments in (
            ["--lifetime-ms", "50"],
            ["--allowed-uid", str(os.geteuid()), "--allowed-gid", str(os.getegid()),
             "--lifetime-ms", "0"],
            ["--allowed-uid", str(os.geteuid()), "--allowed-gid", str(os.getegid()),
             "--lifetime-ms", "30001"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(base + arguments, capture_output=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(list(self.parent.iterdir()), [])

    def test_symlink_and_special_inventory_files_are_not_opened_as_authority(self):
        alias = self.parent / "inventory-alias"
        alias.symlink_to(FIXTURES / "inventory.json")
        fifo = self.parent / "inventory-fifo"
        os.mkfifo(fifo, mode=0o600)
        for inventory in (alias, fifo):
            with self.subTest(inventory=inventory.name):
                result = subprocess.run(
                    [
                        str(BINARY), "--parent", str(self.parent), "--inventory", str(inventory),
                        "--allowed-uid", str(os.geteuid()), "--allowed-gid", str(os.getegid()),
                        "--lifetime-ms", "50",
                    ],
                    capture_output=True, timeout=3,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(set(self.parent.iterdir()), {alias, fifo})

    def test_native_backend_is_explicitly_unavailable(self):
        harness = self.start(backend="native-unavailable")
        with self.assertRaises(InspectorUnavailable):
            client_for(harness.path).inspect("fixture-role")

    def test_fixture_success_cannot_open_production_runtime_channel(self):
        from synai.broker.runtime import RootlessPodmanRuntime
        from synai.broker.registry import BrokerRegistry
        from tests.test_broker import config

        harness = self.start()
        self.assertEqual(client_for(harness.path).inspect("fixture-role").mount_id, 42)
        selected = config(self.parent / "broker")
        registry = BrokerRegistry(selected)
        self.addCleanup(registry.close)
        runtime = RootlessPodmanRuntime(selected, registry)
        with patch("asyncio.create_subprocess_exec") as spawn:
            with self.assertRaises(BrokerError) as denied:
                asyncio.run(runtime._run("info", timeout=1, maximum=1024))
            self.assertEqual(denied.exception.code, "runtime_storage_acceptance_pending")
            spawn.assert_not_called()
        self.assertIsNone(runtime.readiness)

    def test_replay_returns_bound_inconsistent_error(self):
        harness = self.start()
        request = self.request()
        payload = json.dumps(request).encode()
        self.assertEqual(harness.exchange(payload)["status"], "ok")
        response = harness.exchange(payload)
        self.assertEqual(response["challenge"], request["challenge"])
        self.assertEqual(response["status"], "inconsistent")
        self.assertEqual(response["error_code"], "challenge_replayed")
        with self.assertRaises(InspectorInconsistent):
            client_for()._parse_response(response, request["role_id"], request["challenge"])

    def test_server_authenticates_actual_uid_and_gid(self):
        for options in (
            {"allowed_uid": os.geteuid() + 1},
            {"allowed_gid": os.getegid() + 1},
        ):
            with self.subTest(options=options):
                harness = self.start(**options)
                with self.assertRaises((EOFError, OSError)):
                    harness.exchange(json.dumps(self.request()).encode())
                self.assertIsNone(harness.process.poll())

    def test_malformed_request_cannot_select_host_authority(self):
        harness = self.start(lifetime_ms=3500)
        vectors = fixture_document("exchanges.json")
        for index, case in enumerate(vectors["invalid_requests"]):
            if index == 6:
                time.sleep(1.05)
            with self.subTest(case=case["name"]), self.assertRaises((EOFError, OSError)):
                harness.exchange(json.dumps(mutate(vectors["request"], case)).encode())
        self.assertEqual(harness.exchange(json.dumps(self.request()).encode())["status"], "ok")

    def test_malformed_json_and_parser_depth_are_refused(self):
        harness = self.start(lifetime_ms=3500)
        valid = json.dumps(self.request())
        payloads = (
            valid.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1').encode(),
            b'"\xff"', b'{"schema_version":NaN}', b'{"schema_version":18446744073709551616}',
            b'{"schema_version":-18446744073709551617}',
            b"[" * 40 + b"0" + b"]" * 40,
            b'{"schema_version":1.0}', b'{"schema_version":Infinity}', b"{} trailing",
        )
        for index, payload in enumerate(payloads):
            if index == 5:
                time.sleep(1.05)
            with self.subTest(payload=payload), self.assertRaises((EOFError, OSError)):
                harness.exchange(payload)
        self.assertEqual(harness.exchange(json.dumps(self.request()).encode())["status"], "ok")

    def test_oversized_empty_and_truncated_frames_are_refused(self):
        harness = self.start()
        for frame in (
            struct.pack("!I", MAX_FRAME_BYTES + 1),
            struct.pack("!I", 0), b"\x00\x00",
            struct.pack("!I", 100) + b"{}",
        ):
            with self.subTest(frame=frame), harness.connect() as connection:
                connection.sendall(frame)
                connection.shutdown(socket.SHUT_WR)
                try:
                    self.assertEqual(connection.recv(4), b"")
                except ConnectionResetError:
                    pass
        self.assertEqual(harness.exchange(json.dumps(self.request()).encode())["status"], "ok")

    def test_slow_partial_connection_times_out_and_server_recovers(self):
        harness = self.start()
        started = time.monotonic()
        with harness.connect() as connection:
            connection.sendall(b"\x00")
            self.assertEqual(connection.recv(4), b"")
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.1)
        self.assertLess(elapsed, 1)
        self.assertEqual(harness.exchange(json.dumps(self.request()).encode())["status"], "ok")

    def test_connection_flood_is_bounded_and_does_not_crash_server(self):
        harness = self.start()
        accepted = 0
        refused = 0
        for _ in range(20):
            try:
                response = harness.exchange(json.dumps(self.request()).encode())
            except (EOFError, OSError):
                refused += 1
            else:
                self.assertEqual(response["status"], "ok")
                accepted += 1
        self.assertLessEqual(accepted, 10)
        self.assertGreater(refused, 0)
        self.assertIsNone(harness.process.poll())

    def test_clean_lifetime_removes_only_owned_paths(self):
        sentinel = self.parent / "unrelated"
        sentinel.write_text("keep")
        harness = self.start(lifetime_ms=650)
        self.assertEqual(harness.exchange(json.dumps(self.request()).encode())["status"], "ok")
        harness.close()
        self.assertEqual(harness.process.returncode, 0)
        self.assertFalse(harness.path.exists())
        self.assertFalse(harness.path.parent.exists())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_lifetime_bounds_outstanding_slow_connection(self):
        harness = self.start(lifetime_ms=250)
        started = time.monotonic()
        with harness.connect() as connection:
            connection.sendall(b"\x00")
            self.assertEqual(connection.recv(4), b"")
        harness.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(harness.path.exists())

    def test_crash_restart_changes_epoch_and_old_client_refuses_it(self):
        first = self.start()
        client = client_for(first.path)
        first_observation = client.inspect("fixture-role")
        first.process.kill()
        first.process.communicate(timeout=2)
        with self.assertRaises(InspectorUnavailable):
            client.inspect("fixture-role")
        second = self.start()
        self.assertNotEqual(
            first.startup["helper_boot_epoch"], second.startup["helper_boot_epoch"],
        )
        client.socket_path = second.path
        with self.assertRaises(InspectorStale):
            client.inspect("fixture-role")
        self.assertNotEqual(
            first_observation.helper_boot_epoch,
            client_for(second.path).inspect("fixture-role").helper_boot_epoch,
        )


if __name__ == "__main__":
    unittest.main()
