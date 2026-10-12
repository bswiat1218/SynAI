from __future__ import annotations

import asyncio
import json
import os
import selectors
import socket
import stat
import struct
import subprocess
import sys
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

    def test_shared_exact_response_presence_for_every_status(self):
        vectors = fixture_document("exchanges.json")
        request = vectors["request"]
        with patch("synai.broker.storage_inspector.time.time", return_value=1700000000):
            for case in vectors["response_presence_cases"]:
                response = mutate(vectors[case["template"]], case)
                with self.subTest(case=case["name"]):
                    if not case["valid"]:
                        with self.assertRaises(InspectorInvalid):
                            client_for()._parse_response(response, "fixture-role", request["challenge"])
                    elif response["status"] == "ok":
                        client_for()._parse_response(response, "fixture-role", request["challenge"])
                    else:
                        error = InspectorUnavailable if response["status"] == "unavailable" else InspectorInconsistent
                        with self.assertRaises(error):
                            client_for()._parse_response(response, "fixture-role", request["challenge"])

    def test_shared_canonical_escaping_boundary_digests(self):
        for case in fixture_document("canonical_escaping.json")["cases"]:
            role = replace(
                fixture_roles()[0], path=Path("/synthetic/fixture-volume/task" + case["suffix"]),
            )
            with self.subTest(suffix=case["suffix"]):
                self.assertEqual(inventory_generation((role,)), case["generation"])

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


class FixtureSupervisionError(AssertionError):
    def __init__(self, message: str, supervisor: FixtureHarness):
        super().__init__(message)
        self.supervisor = supervisor


class FixtureHarness:
    MAX_STARTUP_BYTES = 4096
    MAX_DIAGNOSTIC_BYTES = 4096

    def __init__(
        self, parent: Path, *, lifetime_ms: int = 1600,
        backend: str = "fixture", allowed_uid: int | None = None,
        allowed_gid: int | None = None, inventory: Path | None = None,
        startup_seconds: float = 5, reap_seconds: float = 1,
    ):
        if startup_seconds <= 0 or reap_seconds <= 0:
            raise ValueError("Fixture supervision deadlines must be positive")
        self.started = time.monotonic()
        self.startup_deadline = self.started + startup_seconds
        self.process_deadline = self.started + max(startup_seconds, lifetime_ms / 1000 + 0.5)
        self.reap_seconds = reap_seconds
        self.unresolved = False
        self.closed = False
        self.diagnostics = bytearray()
        self.stdout_bytes = bytearray()
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
            if self.process.stdout is None or self.process.stderr is None:
                raise AssertionError("Rust fixture harness pipes are unavailable")
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stderr.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
                while b"\n" not in self.stdout_bytes:
                    remaining = self.startup_deadline - time.monotonic()
                    if remaining <= 0:
                        raise AssertionError("Rust fixture harness startup timed out")
                    events = selector.select(remaining)
                    if not events:
                        raise AssertionError("Rust fixture harness startup timed out")
                    for key, _ in events:
                        data = os.read(key.fd, 1024)
                        if not data:
                            selector.unregister(key.fileobj)
                            if key.data == "stdout":
                                raise AssertionError("Rust fixture harness exited before readiness")
                        elif key.data == "stdout":
                            self.stdout_bytes.extend(data)
                            if len(self.stdout_bytes) > self.MAX_STARTUP_BYTES:
                                raise AssertionError("Rust fixture harness startup frame exceeded its limit")
                        else:
                            self._diagnostic_bytes(data)
            line, trailing = bytes(self.stdout_bytes).split(b"\n", 1)
            if trailing.strip():
                raise AssertionError("Rust fixture harness emitted unexpected startup output")
            self.startup = json.loads(
                line.decode("utf-8"), object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            if (
                not isinstance(self.startup, dict)
                or set(self.startup) != {
                    "socket_path", "inventory_generation", "helper_boot_epoch", "fixture_only",
                }
                or self.startup["fixture_only"] is not True
                or not all(isinstance(self.startup[field], str) for field in (
                    "socket_path", "inventory_generation", "helper_boot_epoch",
                ))
            ):
                raise AssertionError("Rust fixture harness readiness schema is invalid")
            self.path = Path(self.startup["socket_path"])
            if not self.path.is_absolute() or not self.path.is_relative_to(parent):
                raise AssertionError("Rust fixture harness socket escaped the disposable parent")
        except BaseException as error:
            try:
                self.close(terminate=True)
            except (AssertionError, OSError) as cleanup_error:
                error.add_note(f"Exact fixture child cleanup unresolved: {cleanup_error}")
                if self.unresolved:
                    raise FixtureSupervisionError(
                        "Fixture startup failed with exact child lifetime unresolved", self,
                    ) from error
            raise

    def _diagnostic_bytes(self, data: bytes):
        remaining = self.MAX_DIAGNOSTIC_BYTES - len(self.diagnostics)
        if remaining > 0:
            self.diagnostics.extend(data[:remaining])

    def _wait_before(self, deadline: float):
        with selectors.DefaultSelector() as selector:
            for pipe in (self.process.stdout, self.process.stderr):
                if pipe is not None and not pipe.closed:
                    selector.register(pipe, selectors.EVENT_READ, pipe is self.process.stderr)
            while self.process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self.process.args, 0)
                for key, _ in selector.select(min(remaining, 0.05)):
                    data = os.read(key.fd, 1024)
                    if not data:
                        selector.unregister(key.fileobj)
                    elif key.data:
                        self._diagnostic_bytes(data)
            self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None and not pipe.closed:
                for _ in range(4):
                    try:
                        data = os.read(pipe.fileno(), 1024)
                    except BlockingIOError:
                        break
                    if not data:
                        break
                    if pipe is self.process.stderr:
                        self._diagnostic_bytes(data)

    def close(self, *, terminate: bool = False):
        if self.closed:
            return
        exceeded = False
        try:
            if terminate and self.process.poll() is None:
                self.process.kill()
            try:
                self._wait_before(
                    time.monotonic() + self.reap_seconds if terminate else self.process_deadline,
                )
            except subprocess.TimeoutExpired:
                exceeded = True
                if self.process.poll() is None:
                    self.process.kill()
                self._wait_before(time.monotonic() + self.reap_seconds)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.unresolved = True
            raise FixtureSupervisionError(
                "Exact fixture child termination or reaping is unresolved", self,
            ) from error
        try:
            for pipe in (self.process.stdout, self.process.stderr):
                if pipe is not None:
                    pipe.close()
        except OSError as error:
            self.unresolved = True
            raise FixtureSupervisionError("Fixture pipe cleanup is unresolved", self) from error
        self.closed = True
        self.unresolved = False
        if exceeded:
            raise AssertionError("Rust fixture harness failed its bounded lifetime")
        if not terminate and self.process.returncode not in (0, -9):
            raise AssertionError(
                f"Rust fixture harness exited with {self.process.returncode}; "
                f"bounded diagnostics: {bytes(self.diagnostics)!r}",
            )

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


class SupervisorLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synai-supervisor-")
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.children = []
        self.real_popen = subprocess.Popen
        self.addCleanup(self.reap_children)

    def reap_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=2)

    def child_factory(self, source):
        def spawn(*args, **kwargs):
            child = self.real_popen(
                [sys.executable, "-c", source], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.children.append(child)
            return child
        return spawn

    def rejected_child(self, source, error=AssertionError):
        started = time.monotonic()
        with patch(
            "tests.test_quota_inspector_prototype.subprocess.Popen",
            side_effect=self.child_factory(source),
        ), self.assertRaises(error):
            FixtureHarness(self.parent, startup_seconds=0.2, reap_seconds=0.5)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(self.children[-1].poll())

    def test_original_partial_startup_reproduction_is_deadline_bounded(self):
        self.rejected_child("import os,time;os.write(1,b'{');time.sleep(30)")

    def test_no_output_is_bounded_and_exact_child_is_reaped(self):
        self.rejected_child("import time;time.sleep(30)")

    def test_partial_utf8_and_invalid_json_are_rejected(self):
        self.rejected_child("import os;os.write(1,b'\\xc3\\n')", UnicodeError)
        self.rejected_child("import os;os.write(1,b'{\\n')", ValueError)

    def test_oversized_output_is_rejected_without_unbounded_capture(self):
        self.rejected_child("import os,time;os.write(1,b'x'*8192);time.sleep(30)")

    def test_stderr_backpressure_is_drained_with_bounded_capture(self):
        self.rejected_child(
            "import os,time;os.write(2,b'x'*262144);os.write(1,b'{');time.sleep(30)",
        )

    def test_successful_startup_after_stderr_flood_retains_only_bounded_diagnostics(self):
        source = "import os;os.write(2,b'x'*262144);" + self.ready_source()
        with patch(
            "tests.test_quota_inspector_prototype.subprocess.Popen",
            side_effect=self.child_factory(source),
        ):
            harness = FixtureHarness(self.parent, startup_seconds=1)
        self.assertEqual(len(harness.diagnostics), harness.MAX_DIAGNOSTIC_BYTES)
        harness.close(terminate=True)

    def test_child_exit_and_broken_stdout_before_readiness(self):
        self.rejected_child("pass")
        self.rejected_child("import os,time;os.close(1);time.sleep(30)")

    def test_spawn_failure_does_not_guess_child_identity(self):
        with patch(
            "tests.test_quota_inspector_prototype.subprocess.Popen",
            side_effect=OSError("injected spawn failure"),
        ), self.assertRaises(OSError):
            FixtureHarness(self.parent, startup_seconds=0.2)
        self.assertEqual(self.children, [])

    def ready_source(self, *, crash=False):
        startup = json.dumps({
            "socket_path": str(self.parent / "fixture.sock"),
            "inventory_generation": inventory_generation(fixture_roles()),
            "helper_boot_epoch": "a" * 32, "fixture_only": True,
        })
        return (
            f"import os,time;os.write(1,{(startup + chr(10)).encode()!r});"
            + ("time.sleep(0.05);os._exit(3)" if crash else "time.sleep(30)")
        )

    def ready_child(self, **options):
        with patch(
            "tests.test_quota_inspector_prototype.subprocess.Popen",
            side_effect=self.child_factory(self.ready_source(**options)),
        ):
            return FixtureHarness(self.parent, startup_seconds=0.2, reap_seconds=0.3)

    def test_repeated_close_is_idempotent_after_exact_child_termination(self):
        harness = self.ready_child()
        harness.close(terminate=True)
        harness.close()
        self.assertTrue(harness.closed)
        self.assertFalse(harness.unresolved)

    def test_fragmented_utf8_valid_startup_is_read_incrementally(self):
        payload = json.dumps({
            "socket_path": str(self.parent / "caf\u00e9.sock"),
            "inventory_generation": inventory_generation(fixture_roles()),
            "helper_boot_epoch": "a" * 32, "fixture_only": True,
        }, ensure_ascii=False).encode() + b"\n"
        split = payload.index(b"\xc3") + 1
        source = (
            f"import os,time;os.write(1,{payload[:split]!r});"
            f"time.sleep(0.02);os.write(1,{payload[split:]!r});time.sleep(30)"
        )
        with patch(
            "tests.test_quota_inspector_prototype.subprocess.Popen",
            side_effect=self.child_factory(source),
        ):
            harness = FixtureHarness(self.parent, startup_seconds=0.3)
        harness.close(terminate=True)
        self.assertEqual(harness.path.name, "caf\u00e9.sock")

    def test_child_crash_after_readiness_is_not_clean_shutdown(self):
        harness = self.ready_child(crash=True)
        with self.assertRaisesRegex(AssertionError, "exited with 3"):
            harness.close()
        self.assertTrue(harness.closed)

    def test_kill_failure_remains_unresolved_and_close_can_retry(self):
        harness = self.ready_child()
        with patch.object(harness.process, "kill", side_effect=OSError("injected kill failure")):
            with self.assertRaisesRegex(AssertionError, "unresolved"):
                harness.close(terminate=True)
        self.assertTrue(harness.unresolved)
        self.assertFalse(harness.closed)
        harness.close(terminate=True)
        self.assertFalse(harness.unresolved)

    def test_reap_uncertainty_is_not_reported_as_clean(self):
        harness = self.ready_child()
        with patch.object(
            harness, "_wait_before", side_effect=subprocess.TimeoutExpired("exact child", 0),
        ), self.assertRaisesRegex(AssertionError, "unresolved"):
            harness.close(terminate=True)
        self.assertTrue(harness.unresolved)
        harness.close(terminate=True)

    def test_pipe_cleanup_failure_is_explicit_and_retryable(self):
        harness = self.ready_child()
        with patch.object(harness.process.stdout, "close", side_effect=OSError("injected close")):
            with self.assertRaisesRegex(FixtureSupervisionError, "pipe cleanup"):
                harness.close(terminate=True)
        self.assertTrue(harness.unresolved)
        self.assertFalse(harness.closed)
        self.assertIsNotNone(harness.process.poll())
        harness.close(terminate=True)
        self.assertTrue(harness.closed)

    def test_constructor_cleanup_error_is_visible_on_original_timeout(self):
        real_close = FixtureHarness.close

        def failing_close(harness, **kwargs):
            real_close(harness, **kwargs)
            raise AssertionError("injected unresolved resource cleanup")

        with patch(
            "tests.test_quota_inspector_prototype.subprocess.Popen",
            side_effect=self.child_factory("import os,time;os.write(1,b'{');time.sleep(30)"),
        ), patch.object(FixtureHarness, "close", failing_close):
            with self.assertRaises(AssertionError) as error:
                FixtureHarness(self.parent, startup_seconds=0.2)
        self.assertIn("cleanup unresolved", " ".join(error.exception.__notes__))
        self.assertIsNotNone(self.children[-1].poll())

    def test_constructor_unresolved_child_ownership_is_retained_for_retry(self):
        real_factory = self.child_factory("import os,time;os.write(1,b'{');time.sleep(30)")
        injected = []

        def spawn(*args, **kwargs):
            child = real_factory(*args, **kwargs)
            injected.append(patch.object(child, "kill", side_effect=OSError("injected failure")))
            injected[-1].start()
            return child

        try:
            with patch(
                "tests.test_quota_inspector_prototype.subprocess.Popen", side_effect=spawn,
            ), self.assertRaises(FixtureSupervisionError) as error:
                FixtureHarness(self.parent, startup_seconds=0.1, reap_seconds=0.2)
            self.assertTrue(error.exception.supervisor.unresolved)
            self.assertIs(error.exception.supervisor.process, self.children[-1])
        finally:
            for injection in injected:
                injection.stop()
        error.exception.supervisor.close(terminate=True)
        self.assertFalse(error.exception.supervisor.unresolved)


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

    def raw_arguments(self, lifetime_ms=150):
        return [
            str(BINARY), "--parent", str(self.parent),
            "--inventory", str(FIXTURES / "inventory.json"),
            "--allowed-uid", str(os.geteuid()), "--allowed-gid", str(os.getegid()),
            "--lifetime-ms", str(lifetime_ms),
        ]

    def test_original_blocked_stdout_lifetime_and_cleanup_are_corrected(self):
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(write_fd, False)
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(write_fd, True)
            started = time.monotonic()
            child = subprocess.Popen(self.raw_arguments(), stdout=write_fd, stderr=subprocess.PIPE)
            try:
                child.wait(timeout=1.5)
                self.assertLess(time.monotonic() - started, 1)
                self.assertNotEqual(child.returncode, 0)
                self.assertEqual(list(self.parent.iterdir()), [])
                self.assertNotIn(b"panicked", child.stderr.read(4096))
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=1)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_original_broken_stdout_panic_cleanup_is_corrected(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            child = subprocess.Popen(self.raw_arguments(), stdout=write_fd, stderr=subprocess.PIPE)
            try:
                _, error = child.communicate(timeout=1.5)
                self.assertNotEqual(child.returncode, 0)
                self.assertNotEqual(child.returncode, 101)
                self.assertNotIn(b"panicked", error)
                self.assertLess(len(error), 1024)
                self.assertEqual(list(self.parent.iterdir()), [])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=1)
        finally:
            os.close(write_fd)

    def test_blocked_stderr_cannot_trap_error_reporting(self):
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(write_fd, False)
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(write_fd, True)
            child = subprocess.Popen(
                [str(BINARY), "--invalid", "argument"], stdout=subprocess.PIPE, stderr=write_fd,
            )
            try:
                child.wait(timeout=1.5)
                self.assertNotEqual(child.returncode, 0)
                self.assertEqual(list(self.parent.iterdir()), [])
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=1)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_startup_deadline_with_rust_directory_cleanup_failure_is_explicit(self):
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(write_fd, False)
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(write_fd, True)
            child = subprocess.Popen(self.raw_arguments(500), stdout=write_fd, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 0.3
                directories = []
                while not directories and time.monotonic() < deadline:
                    directories = list(self.parent.glob("quota-inspector-*"))
                    time.sleep(0.005)
                self.assertEqual(len(directories), 1)
                sentinel = directories[0] / "foreign-content"
                sentinel.write_text("do not remove")
                _, error = child.communicate(timeout=1.5)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn(b"cleanup", error.lower())
                self.assertNotIn(b"panicked", error)
                self.assertTrue(sentinel.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=1)
        finally:
            os.close(read_fd)
            os.close(write_fd)
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

    def test_rejected_flood_near_lifetime_does_not_extend_process_budget(self):
        harness = self.start(lifetime_ms=250)
        for _ in range(100):
            try:
                with harness.connect():
                    pass
            except OSError:
                break
        harness.close()
        self.assertFalse(harness.path.exists())
        self.assertTrue(harness.closed)

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

    def test_supervisor_close_while_child_has_active_partial_connection(self):
        harness = self.start(lifetime_ms=1500)
        with harness.connect() as connection:
            connection.sendall(b"\x00")
            harness.close(terminate=True)
        self.assertTrue(harness.closed)
        self.assertIsNotNone(harness.process.poll())

    def test_restart_does_not_inherit_replay_entries(self):
        payload = json.dumps(self.request("d" * 64)).encode()
        first = self.start(lifetime_ms=200)
        self.assertEqual(first.exchange(payload)["status"], "ok")
        first.close()
        second = self.start(lifetime_ms=200)
        self.assertEqual(second.exchange(payload)["status"], "ok")
        self.assertNotEqual(first.startup["helper_boot_epoch"], second.startup["helper_boot_epoch"])

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
