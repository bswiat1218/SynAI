from __future__ import annotations

import asyncio
import base64
import hashlib
import httpx
import io
import json
import os
import queue
import socket
import stat
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from synai.client.cli import _pair, _run_connection
from synai.client.protocol import ClientProtocolError, canonical_json, signed_http_request
from synai.client.state import ClientState, ClientStateError
from synai.client.workspace import WorkspaceError, WorkspaceRegistry, capture_workspace
from synai.web.app import create_app
from synai.web.config import WebConfig
from synai.web.distributed import DistributedRegistry


PASSWORD = "client integration passphrase"


class _Provider:
    async def list_models(self):
        return []

    async def capabilities(self, name: str):
        raise AssertionError(name)

    async def chat(self, model: str, messages: list, tools: list):
        raise AssertionError("Client Agent integration must not use chat tools")
        yield


class _Core:
    def __init__(self, root: Path, port: int) -> None:
        self.config = WebConfig(
            root,
            (),
            initial_password=PASSWORD,
            public_origin=f"http://127.0.0.1:{port}",
            port=port,
        )
        self.app = create_app(self.config, provider=_Provider())
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=port, log_level="critical",
            access_log=False, lifespan="on",
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            if not self.thread.is_alive():
                raise RuntimeError("Disposable SynAI Core failed to start.")
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("Timed out starting disposable SynAI Core.")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("Disposable SynAI Core did not stop.")


class _ClientRunner:
    def __init__(self, state: ClientState) -> None:
        self.state = state
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[None] | None = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("Client Agent connection loop did not start.")

    def cancel(self) -> None:
        if self.loop is not None and self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Client Agent connection loop did not stop.")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self.task = loop.create_task(_run_connection(self.state, once=False))
        self.ready.set()
        try:
            loop.run_until_complete(self.task)
        except BaseException as exc:
            self.error = exc
        finally:
            loop.close()
            self.done.set()


class ClientStateAndWorkspaceTests(unittest.TestCase):
    def test_private_state_permissions_and_symlink_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "state"
            state = ClientState(home)
            state.initialize()
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            state.write_identity({"schema_version": 1, "private_key_pem": "secret"})
            self.assertEqual(stat.S_IMODE(state.identity_path.stat().st_mode), 0o600)
            os.chmod(state.identity_path, 0o644)
            with self.assertRaises(ClientStateError):
                state.read_identity()
            os.chmod(state.identity_path, 0o600)
            state.identity_path.unlink()
            target = Path(temporary) / "target.json"
            target.write_text("{}", encoding="utf-8")
            state.identity_path.symlink_to(target)
            with self.assertRaises(ClientStateError):
                state.read_identity()
            os.chmod(home, 0o755)
            with self.assertRaises(ClientStateError):
                state.read_config()

    def test_preview_excludes_secrets_symlinks_and_unsupported_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "src").mkdir()
            (workspace / "src" / "module.py").write_text("def stable():\n    return 7\n", encoding="utf-8")
            (workspace / ".env").write_text("TOKEN=not-for-upload", encoding="utf-8")
            (workspace / "private.key").write_text("private material", encoding="utf-8")
            (workspace / "archive.zip").write_bytes(b"not supported")
            (workspace / "external.py").symlink_to(root / "outside.py")
            (root / "outside.py").write_text("outside", encoding="utf-8")

            state = ClientState(root / "client-state")
            registry = WorkspaceRegistry(state)
            binding = registry.add("a" * 32, "b" * 32, "test alias", workspace)
            preview = capture_workspace(binding)
            self.assertEqual([item.path for item in preview.files], ["src/module.py"])
            self.assertEqual(preview.files[0].sha256, hashlib.sha256(b"def stable():\n    return 7\n").hexdigest())
            excluded_paths = {path for path, _ in preview.excluded}
            rejected_paths = {path for path, _ in preview.rejected}
            self.assertTrue({".env", "private.key", "external.py"} <= excluded_paths)
            self.assertIn("archive.zip", rejected_paths)
            registry.remove("b" * 32)
            with self.assertRaises(WorkspaceError):
                registry.find("b" * 32)

    def test_workspace_rejects_symlink_path_components_and_duplicate_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real)
            registry = WorkspaceRegistry(ClientState(root / "state"))
            with self.assertRaises(WorkspaceError):
                registry.add("a" * 32, "b" * 32, "workspace", alias)
            registry.add("a" * 32, "b" * 32, "workspace", real)
            with self.assertRaises(WorkspaceError):
                registry.add("a" * 32, "b" * 32, "duplicate", real)


class ClientAgentNetworkIntegrationTests(unittest.TestCase):
    def test_pair_connect_preview_deny_upload_restart_and_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            core = _Core(root / "core-data", port)
            core.start()
            self.addCleanup(lambda: core.stop() if core.thread.is_alive() else None)

            admin = httpx.Client(base_url=base_url, timeout=5, follow_redirects=False)
            self.addCleanup(admin.close)
            login = admin.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": base_url},
            )
            self.assertEqual(login.status_code, 200, login.text)
            csrf = login.json()["csrf_token"]
            admin_headers = {"Origin": base_url, "X-CSRF-Token": csrf}

            services = core.app.state.services
            assert isinstance(services.distributed, DistributedRegistry)
            challenge = services.distributed.create_pairing_challenge()
            state = ClientState(root / "client-state")
            state.write_config({"schema_version": 1, "server_url": base_url})
            with patch("builtins.input", side_effect=[challenge.challenge_id, "PAIR"]), patch(
                "getpass.getpass", return_value=challenge.secret,
            ), redirect_stdout(io.StringIO()):
                _pair(state)
            identity = state.read_identity()
            device_id = identity["device_id"]
            self.assertEqual(identity["server_url"], base_url)

            authorized = admin.post(f"/api/v1/devices/{device_id}/authorize", headers=admin_headers)
            self.assertEqual(authorized.status_code, 200, authorized.text)
            project = services.distributed.create_project("Integration project", "integration-project-key-01")
            binding = services.distributed.create_binding(
                str(project.project_id), device_id, "Local source",
            )
            source = root / "source"
            (source / "src").mkdir(parents=True)
            content = b"def integration_symbol():\n    return 'immutable'\n"
            source_file = source / "src" / "module.py"
            source_file.write_bytes(content)
            (source / ".env").write_text("TOKEN=excluded", encoding="utf-8")
            workspace = WorkspaceRegistry(state).add(
                str(project.project_id), str(binding.binding_id), "Local source", source,
            )

            answers: queue.Queue[str] = queue.Queue()
            prompted = threading.Event()

            def local_confirmation(_: str) -> str:
                prompted.set()
                return answers.get(timeout=20)

            runner = _ClientRunner(state)
            with patch("builtins.input", side_effect=local_confirmation), redirect_stdout(io.StringIO()):
                runner.start()
                self.addCleanup(lambda: runner.cancel() if runner.thread.is_alive() else None)
                self._wait_for(lambda: services.device_connections.connected(device_id))
                devices = admin.get("/api/v1/devices")
                self.assertTrue(next(item for item in devices.json()["devices"] if item["id"] == device_id)["connected"])

                answers.put("NO")
                prompted.clear()
                denied = admin.post(
                    f"/api/v1/logical-projects/{project.project_id}/bindings/{binding.binding_id}/snapshot-requests",
                    headers=admin_headers,
                )
                self.assertEqual(denied.status_code, 202, denied.text)
                self.assertTrue(prompted.wait(5))
                self._wait_for(lambda: not services.device_connections._active[device_id].pending_operations)
                snapshots = admin.get(f"/api/v1/logical-projects/{project.project_id}/snapshots")
                self.assertEqual(snapshots.status_code, 200)
                self.assertEqual(snapshots.json()["snapshots"], [])
                with services.database.connect() as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM snapshot_uploads").fetchone()[0], 0)

                answers.put("UPLOAD")
                prompted.clear()
                approved = admin.post(
                    f"/api/v1/logical-projects/{project.project_id}/bindings/{binding.binding_id}/snapshot-requests",
                    headers=admin_headers,
                )
                self.assertEqual(approved.status_code, 202, approved.text)
                self.assertTrue(prompted.wait(5))
                self._wait_for(lambda: bool(
                    admin.get(f"/api/v1/logical-projects/{project.project_id}/snapshots").json()["snapshots"]
                ))
                snapshots = admin.get(f"/api/v1/logical-projects/{project.project_id}/snapshots").json()["snapshots"]
                manifest = snapshots[0]["manifest"]
                self.assertEqual(manifest["files"][0]["path"], "src/module.py")
                self.assertNotIn(str(source), json.dumps(manifest))
                self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(
                    hashlib.sha256(canonical_json(manifest)).hexdigest(),
                    snapshots[0]["manifest_digest"],
                )
                self.assertEqual(manifest["workspace_binding_id"], str(binding.binding_id))
                self.assertEqual(manifest["project_id"], str(project.project_id))
                self.assertEqual(manifest["source_device_id"], device_id)

                core.stop()
                core = _Core(root / "core-data", port)
                core.start()
                services = core.app.state.services
                self._wait_for(lambda: services.device_connections.connected(device_id), timeout=20)

                revoked = admin.delete(f"/api/v1/devices/{device_id}", headers=admin_headers)
                self.assertEqual(revoked.status_code, 204, revoked.text)
                self._wait_for(lambda: runner.done.is_set(), timeout=10)
                self.assertIsNotNone(runner.error)
                self.assertFalse(services.device_connections.connected(device_id))

                key = serialization.load_pem_private_key(
                    identity["private_key_pem"].encode("ascii"), password=None,
                )
                body = canonical_json({
                    "project_id": str(project.project_id),
                    "binding_id": str(binding.binding_id),
                    "idempotency_key": "revoked-device-upload-key",
                    "files": [],
                })
                from synai.client.protocol import api_client

                with api_client(base_url) as device_http:
                    with self.assertRaises(ClientProtocolError):
                        signed_http_request(
                            device_http, identity, key, "POST",
                            f"/api/v1/device/{device_id}/snapshot-uploads", body,
                        )

    @staticmethod
    def _wait_for(predicate, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        raise AssertionError("Timed out waiting for expected disposable integration state.")
