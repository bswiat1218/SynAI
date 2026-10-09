from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from synai.config import Settings
from synai.preferences import Preferences, PreferencesStore, resolve_connection


class PreferencesTests(unittest.TestCase):
    def test_private_roundtrip_and_atomic_failure_preserve_saved_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory) / "data")
            self.assertEqual(store.load(), Preferences())
            value = Preferences("http://server:11434", 55, "textual-light")
            store.save(value)
            self.assertEqual(store.load(), value)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.storage.root.stat().st_mode & 0o777, 0o700)
            with patch("pathlib.Path.replace", side_effect=OSError("write failed")):
                with self.assertRaisesRegex(OSError, "write failed"):
                    store.save(replace(value, theme="textual-dark"))
            self.assertEqual(store.load(), value)
            self.assertEqual({path.name for path in store.storage.root.iterdir()}, {"settings.json", "conversations"})

    def test_each_field_has_independent_explicit_precedence(self) -> None:
        settings = Settings(ollama_url="http://localhost:11434", request_timeout=1200)
        saved = Preferences("http://saved:11434", 65)
        values, sources = resolve_connection(settings, saved, environ={})
        self.assertEqual((values.ollama_url, values.request_timeout), ("http://saved:11434", 65))
        env = {"OLLAMA_URL": "http://url:11434/", "OLLAMA_HOST": "http://host:11434",
               "AGENT_REQUEST_TIMEOUT": "70", "BENCHMARK_REQUEST_TIMEOUT": "80"}
        values, sources = resolve_connection(settings, saved, environ=env, cli_timeout=1200)
        self.assertEqual((values.ollama_url, values.request_timeout), ("http://url:11434", 1200))
        self.assertEqual(sources, {"ollama_url": "environment", "request_timeout": "CLI"})
        values, _ = resolve_connection(settings, saved, environ={"AGENT_REQUEST_TIMEOUT": "invalid"},
                                       cli_url="http://localhost:11434", cli_timeout=1200)
        self.assertEqual((values.ollama_url, values.request_timeout), ("http://localhost:11434", 1200))
        values, _ = resolve_connection(settings, saved, environ={
            "OLLAMA_HOST": "http://host:11434", "BENCHMARK_REQUEST_TIMEOUT": "80",
        })
        self.assertEqual((values.ollama_url, values.request_timeout), ("http://host:11434", 80))

    def test_malformed_and_unsafe_settings_are_not_silently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory))
            store.save(Preferences())
            original = asdict(Preferences())
            cases = [
                "{bad", "[]", json.dumps({}),
                json.dumps(dict(original, schema_version=True)),
                json.dumps(dict(original, request_timeout=True)),
                json.dumps(dict(original, request_timeout=float("nan"))),
                json.dumps(dict(original, ollama_url="http://user:secret@server")),
                json.dumps(dict(original, theme=None)),
            ]
            for contents in cases:
                with self.subTest(contents=contents):
                    store.path.write_text(contents)
                    with self.assertRaisesRegex(ValueError, "Cannot load"):
                        store.load()
                    self.assertEqual(store.path.read_text(), contents)
            store.save(Preferences())
            store.path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private file"):
                store.load()
            store.path.unlink()
            outside = Path(directory) / "outside.json"
            outside.write_text("{}")
            store.path.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "Symlink"):
                store.load()
            with self.assertRaisesRegex(ValueError, "Symlink"):
                store.save(Preferences())
            self.assertEqual(outside.read_text(), "{}")

    def test_legacy_preferences_keep_project_memory_opted_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory))
            store.storage.initialize()
            store.path.write_text(json.dumps({
                "schema_version": 1,
                "ollama_url": "http://localhost:11434",
                "request_timeout": 1200,
                "theme": "synai-cyberpunk",
            }))
            store.path.chmod(0o600)
            loaded = store.load()
            self.assertFalse(loaded.project_memory.enabled)
            self.assertFalse(loaded.project_memory.automatic_capture)
