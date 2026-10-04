from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from synai.config import ConversationEnvironment, Settings
from synai.history import History, HistoryError
from synai.models import Session


class EnvironmentTests(unittest.TestCase):
    def test_execution_mode_roundtrip_and_strict_legacy_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = History(root, replace(Settings(), execution_mode="host"))
            for mode in ("host", "sandbox"):
                settings = replace(Settings(), execution_mode=mode)
                session = Session("m", settings.ollama_url, directory)
                session.set_environment(ConversationEnvironment.from_settings(settings, root))
                history.save(session)
                path = root / f"{session.session_id}.json"
                self.assertEqual(history.load(path).environment.execution_mode, mode)
                self.assertEqual(history.load(path).schema_version, 5)
                data = json.loads(path.read_text())
                del data["environment"]["execution_mode"]
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(HistoryError, "requires execution mode"):
                    history.load(path)
                data["schema_version"] = 2
                data["environment"]["ollama_url"] = session.endpoint
                data["environment"]["request_timeout"] = settings.request_timeout
                path.write_text(json.dumps(data))
                legacy = history.load(path)
                self.assertEqual(legacy.environment.execution_mode, "sandbox")
                history.save(legacy)
                self.assertEqual(history.load(path).schema_version, 5)
                data["environment"].pop("image")
                path.write_text(json.dumps(data))
                with self.assertRaises(HistoryError):
                    history.load(path)
            legacy = Session("old", Settings().ollama_url, directory)
            self.assertEqual(history.environment_for(legacy).execution_mode, "sandbox")

    def test_full_snapshot_roundtrip_and_legacy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings(), tool_budget=11, runtime="podman", request_timeout=65)
            history = History(Path(directory), settings)
            env = ConversationEnvironment.from_settings(settings, Path(directory))
            session = Session("m", settings.ollama_url, directory)
            session.set_environment(env)
            history.save(session)
            self.assertEqual(history.load(Path(directory) / f"{session.session_id}.json").environment, env)
            legacy = Session("old", "http://old:11434", directory, limits={"tool_budget": 3})
            history.save(legacy)
            data = json.loads((Path(directory) / f"{legacy.session_id}.json").read_text())
            data.pop("environment")
            (Path(directory) / f"{legacy.session_id}.json").write_text(json.dumps(data))
            loaded = history.load(Path(directory) / f"{legacy.session_id}.json")
            self.assertIsNone(loaded.environment)
            fallback = history.environment_for(loaded)
            self.assertNotIn("ollama_url", asdict(fallback))
            self.assertEqual(fallback.tool_budget, 3)
            self.assertEqual(fallback.runtime, "podman")
            self.assertEqual(fallback.settings(settings).request_timeout, 65)

    def test_invalid_saved_environment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory))
            env = ConversationEnvironment.from_settings(Settings(), Path(directory))
            session = Session("m", Settings().ollama_url, directory)
            session.set_environment(env)
            history.save(session)
            path = Path(directory) / f"{session.session_id}.json"
            original = json.loads(path.read_text())
            cases = {
                "tool_budget": True, "output_bytes": 3, "request_timeout": float("nan"),
                "runtime": "host", "workspace": "relative", "image": "-invalid",
                "cpus": "two", "pids": 0, "memory": "unbounded", "ollama_url": "file:///etc",
                "execution_mode": "unsafe",
            }
            for key, value in cases.items():
                with self.subTest(key=key):
                    data = json.loads(json.dumps(original))
                    data["environment"][key] = value
                    path.write_text(json.dumps(data))
                    with self.assertRaises(HistoryError):
                        history.load(path)
            data = dict(original, endpoint="http://different:11434")
            path.write_text(json.dumps(data))
            self.assertEqual(history.load(path).endpoint, "http://different:11434")
            data = dict(original, environment=None)
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(HistoryError, "requires"):
                history.load(path)
            data = json.loads(json.dumps(original))
            data["environment"]["execution_mode"] = "host"
            data["container_id"] = "stale-container"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(HistoryError, "cannot reference"):
                history.load(path)

    def test_snapshot_excludes_app_history_location_and_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = ConversationEnvironment.from_settings(Settings(), Path(directory))
            self.assertNotIn("history_dir", asdict(env))
            self.assertNotIn("ollama_url", asdict(env))
            self.assertNotIn("request_timeout", asdict(env))
            settings = replace(Settings(), history_dir=Path(directory) / "new-history")
            self.assertEqual(env.settings(settings).history_dir, settings.history_dir)
