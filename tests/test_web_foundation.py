from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from synai.models import ModelInfo
from synai.providers.base import ProviderError
from synai.web.app import create_app
from synai.web.config import WebConfig, WorkspaceMountConfig
from synai.web.database import MetadataDatabase


ORIGIN = "http://127.0.0.1:8765"
PASSWORD = "correct horse battery staple"


class FakeProvider:
    def __init__(self, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.closed = False

    async def list_models(self) -> list[ModelInfo]:
        if self.unavailable:
            raise ProviderError("private provider endpoint detail")
        return [ModelInfo("test-model")]

    async def capabilities(self, name: str) -> ModelInfo:
        return ModelInfo(name)

    async def chat(self, model: str, messages: list, tools: list):
        raise AssertionError("Web Phase 13B must not expose chat execution")
        yield  # pragma: no cover

    async def close(self) -> None:
        self.closed = True


class WebFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_root = self.root / "data"
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.config = WebConfig(
            data_root=self.data_root,
            workspace_mounts=(WorkspaceMountConfig("main", self.workspace),),
            initial_password=PASSWORD,
        )
        self.provider = FakeProvider()
        self.app = create_app(self.config, provider=self.provider)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)

    def login(self, password: str = PASSWORD, *, origin: str = ORIGIN):
        return self.client.post(
            "/api/v1/auth/login",
            json={"password": password},
            headers={"Origin": origin},
        )

    def authenticated(self):
        response = self.login()
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    def test_lifespan_health_auth_and_provider_close(self) -> None:
        health = self.client.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "alive"})
        self.assertEqual(self.client.get("/api/v1/ready").json(), {"status": "ready"})
        self.assertEqual(self.client.get("/api/v1/models").status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        response = self.client.get("/api/v1/models")
        self.assertEqual(response.json(), {"models": [{"name": "test-model"}]})
        self.assertFalse(self.provider.closed)

    def test_shutdown_closes_provider(self) -> None:
        provider = FakeProvider()
        app = create_app(
            WebConfig(self.root / "shutdown-data", (), initial_password=PASSWORD),
            provider=provider,
        )
        with TestClient(app):
            self.assertFalse(provider.closed)
        self.assertTrue(provider.closed)

    def test_login_failure_rate_limit_and_secret_redaction(self) -> None:
        failed = self.login("wrong password must not be reflected")
        self.assertEqual(failed.status_code, 401)
        self.assertNotIn("wrong password", failed.text)
        self.assertNotIn("password_hash", failed.text)
        for _ in range(4):
            response = self.login("definitely wrong")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.login("definitely wrong").status_code, 429)

    def test_login_origin_session_expiry_csrf_and_revocation(self) -> None:
        self.assertEqual(self.login(origin="https://attacker.example").status_code, 403)
        csrf = self.authenticated()
        cookie = self.client.cookies.get("synai_session")
        self.assertIsNotNone(cookie)
        database = self.app.state.services.database
        with database.connect() as connection:
            stored_token_hash = connection.execute(
                "SELECT token_hash FROM sessions",
            ).fetchone()["token_hash"]
            credential_hash = connection.execute(
                "SELECT password_hash FROM credentials WHERE singleton = 1",
            ).fetchone()["password_hash"]
        self.assertNotEqual(stored_token_hash, cookie)
        self.assertTrue(credential_hash.startswith("$argon2id$"))
        cookie_item = next(
            (item for item in self.client.cookies.jar if item.name == "synai_session"),
        )
        self.assertTrue(cookie_item.has_nonstandard_attr("HttpOnly"))
        self.assertEqual(cookie_item.get_nonstandard_attr("SameSite"), "strict")
        self.assertEqual(self.client.get("/api/v1/auth/session").status_code, 200)
        self.assertEqual(self.client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://attacker.example", "X-CSRF-Token": csrf},
        ).status_code, 403)
        logged_out = self.client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        self.assertEqual(logged_out.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/session").status_code, 401)

        self.authenticated()
        database = self.app.state.services.database
        with database.connect() as connection:
            connection.execute("UPDATE sessions SET expires_at = 1")
        self.assertEqual(self.client.get("/api/v1/auth/session").status_code, 401)

    def test_csrf_refresh_and_credential_rotation_revoke_sessions(self) -> None:
        self.authenticated()
        csrf = self.client.post(
            "/api/v1/auth/csrf", headers={"Origin": ORIGIN},
        ).json()["csrf_token"]
        result = self.client.post(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "a different long passphrase"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        self.assertEqual(result.status_code, 204)
        self.assertEqual(self.client.get("/api/v1/auth/session").status_code, 401)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login("a different long passphrase").status_code, 200)

    def test_ollama_unavailable_does_not_affect_liveness_or_disclose_details(self) -> None:
        unavailable = FakeProvider(unavailable=True)
        with tempfile.TemporaryDirectory() as data:
            app = create_app(
                WebConfig(Path(data), (), initial_password=PASSWORD),
                provider=unavailable,
            )
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
                self.assertEqual(client.post(
                    "/api/v1/auth/login", json={"password": PASSWORD},
                    headers={"Origin": ORIGIN},
                ).status_code, 200)
                result = client.get("/api/v1/models")
                self.assertEqual(result.status_code, 503)
                self.assertNotIn("private provider endpoint", result.text)

    def test_project_registration_is_allowlisted_opaque_and_read_only(self) -> None:
        csrf = self.authenticated()
        forbidden = self.client.post(
            "/api/v1/projects",
            json={"workspace_key": "outside"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        self.assertEqual(forbidden.status_code, 404)
        arbitrary_path = self.client.post(
            "/api/v1/projects",
            json={"workspace_key": "main", "path": str(self.root)},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        self.assertEqual(arbitrary_path.status_code, 422)
        registered = self.client.post(
            "/api/v1/projects",
            json={"workspace_key": "main"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertEqual(registered.json()["access"], "read_only")
        self.assertEqual(registered.json()["compatibility_state"], "legacy_host_path")
        self.assertNotIn(str(self.workspace), registered.text)
        identifier = registered.json()["id"]
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{identifier}").json()["id"], identifier,
        )
        self.assertEqual(len(self.client.get("/api/v1/projects").json()["projects"]), 1)
        self.assertEqual(
            self.client.get("/api/v1/projects/00000000000000000000000000000000").status_code,
            404,
        )
        paths = self.app.openapi()["paths"]
        self.assertIn("/api/v1/chat/sessions", paths)
        self.assertFalse(any("/tools" in path or "/execute" in path for path in paths))
        project_methods = {
            method
            for path, methods in self.app.openapi()["paths"].items()
            if path.startswith("/api/v1/projects")
            for method in methods
        }
        self.assertEqual(project_methods, {"get", "post"})

    def test_similar_project_names_have_distinct_ids(self) -> None:
        left = self.root / "left" / "shared"
        right = self.root / "right" / "shared"
        left.mkdir(parents=True)
        right.mkdir(parents=True)
        app = create_app(
            WebConfig(
                self.root / "other-data",
                (
                    WorkspaceMountConfig("left", left),
                    WorkspaceMountConfig("right", right),
                ),
                initial_password=PASSWORD,
            ),
            provider=FakeProvider(),
        )
        with TestClient(app) as client:
            csrf = client.post(
                "/api/v1/auth/login", json={"password": PASSWORD},
                headers={"Origin": ORIGIN},
            ).json()["csrf_token"]
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            one = client.post("/api/v1/projects", json={"workspace_key": "left"}, headers=headers).json()
            two = client.post("/api/v1/projects", json={"workspace_key": "right"}, headers=headers).json()
            self.assertEqual(one["name"], two["name"])
            self.assertNotEqual(one["id"], two["id"])

    def test_symlink_home_root_replacement_and_request_limits(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        symlink = self.root / "workspace-link"
        symlink.symlink_to(outside, target_is_directory=True)
        bad_app = create_app(
            WebConfig(
                self.root / "symlink-data",
                (WorkspaceMountConfig("escape", symlink),),
                initial_password=PASSWORD,
            ),
            provider=FakeProvider(),
        )
        with self.assertRaises(ValueError), TestClient(bad_app):
            pass

        home_app = create_app(
            WebConfig(
                self.root / "home-data",
                (WorkspaceMountConfig("home", Path.home()),),
                initial_password=PASSWORD,
            ),
            provider=FakeProvider(),
        )
        with self.assertRaises(ValueError), TestClient(home_app):
            pass
        root_app = create_app(
            WebConfig(
                self.root / "root-data",
                (WorkspaceMountConfig("root", Path("/")),),
                initial_password=PASSWORD,
            ),
            provider=FakeProvider(),
        )
        with self.assertRaises(ValueError), TestClient(root_app):
            pass

        csrf = self.authenticated()
        registered = self.client.post(
            "/api/v1/projects",
            json={"workspace_key": "main"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        ).json()
        previous = self.workspace.with_name("project-old")
        self.workspace.rename(previous)
        self.workspace.mkdir()
        inspected = self.client.get(f"/api/v1/projects/{registered['id']}")
        self.assertEqual(inspected.json()["status"], "stale")

        limited = create_app(
            WebConfig(
                self.root / "limited-data", (), initial_password=PASSWORD,
                request_limit_bytes=1024,
            ),
            provider=FakeProvider(),
        )
        with TestClient(limited) as client:
            response = client.post(
                "/api/v1/auth/login",
                content=b"x" * 1025,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"]["code"], "request_too_large")

    def test_missing_and_invalid_initial_configuration_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as data:
            app = create_app(
                WebConfig(Path(data), (), initial_password=None),
                provider=FakeProvider(),
            )
            with self.assertRaisesRegex(ValueError, "SYNAI_INITIAL_PASSWORD"):
                with TestClient(app):
                    pass
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            WebConfig(
                self.data_root,
                (),
                initial_password=PASSWORD,
                public_origin="http://example.com",
            ).validate()

    def test_metadata_schema_v1_migrates_to_current_version(self) -> None:
        legacy_root = self.root / "legacy-data"
        legacy_root.mkdir(mode=0o700)
        legacy_dir = legacy_root / "web"
        legacy_dir.mkdir(mode=0o700)
        legacy_db = legacy_dir / "metadata.sqlite3"
        with sqlite3.connect(legacy_db) as connection:
            connection.executescript(
                """
                CREATE TABLE credentials (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    password_hash TEXT NOT NULL
                );
                CREATE TABLE sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX sessions_expiry ON sessions(expires_at);
                CREATE TABLE login_limits (
                    peer_hash TEXT PRIMARY KEY,
                    window_start INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    blocked_until INTEGER NOT NULL
                );
                CREATE INDEX login_limits_age ON login_limits(window_start);
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    workspace_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    device_id INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    owner_uid INTEGER NOT NULL,
                    registered_at INTEGER NOT NULL
                );
                CREATE TABLE workspace_lease_generations (
                    workspace_key TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    acquired_at INTEGER NOT NULL,
                    released_at INTEGER,
                    status TEXT NOT NULL,
                    PRIMARY KEY (workspace_key, generation)
                );
                CREATE INDEX lease_latest ON workspace_lease_generations(
                    workspace_key, generation DESC
                );
                PRAGMA user_version = 1;
                """
            )
        legacy_root.chmod(0o700)
        legacy_dir.chmod(0o700)
        legacy_db.chmod(0o600)
        database = MetadataDatabase(legacy_root)
        database.initialize()
        with database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(workspace_lease_generations)",
                ).fetchall()
            }
        self.assertEqual(version, MetadataDatabase.SCHEMA_VERSION)
        self.assertIn("label", columns)

    def test_https_cookie_is_secure_and_csrf_is_origin_bound(self) -> None:
        config = WebConfig(
            self.root / "https-data",
            (),
            initial_password=PASSWORD,
            public_origin="https://synai.example",
        )
        app = create_app(config, provider=FakeProvider())
        with TestClient(app, base_url="https://synai.example") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": "https://synai.example"},
            )
            self.assertEqual(login.status_code, 200)
            cookie = login.headers["set-cookie"].lower()
            self.assertIn("secure", cookie)
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=strict", cookie)
            rejected = client.post(
                "/api/v1/auth/csrf",
                headers={"Origin": "https://attacker.example"},
            )
            self.assertEqual(rejected.status_code, 403)
