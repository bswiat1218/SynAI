from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from synai import __version__
from synai.cli import main
from synai.preferences import Preferences
from synai.storage import CHECKOUT, SOURCE, ConversationStorage, source_overlap


class CliTests(unittest.TestCase):
    def test_early_exits_do_not_import_application_or_create_storage(self) -> None:
        for option in ("--help", "--version", "--print-editor-image-recipe"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, "-c",
                     "import sys; from synai.cli import main; "
                     f"main([{option!r}]); "
                     "assert 'synai.tui.application' not in sys.modules; "
                     "assert 'synai.preferences' not in sys.modules; "
                     "assert 'gi' not in sys.modules"],
                    env={**os.environ, "HOME": directory},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((Path(directory) / ".synai").exists())
                self.assertTrue(result.stdout.strip())

    def test_recipe_is_exact_packaged_resource(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--print-editor-image-recipe"])
        self.assertEqual(output.getvalue(), files("synai.editor").joinpath(
            "sandbox-editor.Dockerfile").read_text(encoding="utf-8"))
        self.assertIn("FROM python:3.12-slim-bookworm", output.getvalue())
        self.assertNotIn("apt-get", output.getvalue())
        self.assertNotIn("git clone", output.getvalue())

    def test_version_uses_authoritative_package_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["--version"])
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue(), f"SynAI {__version__}\n")

    def test_repository_launcher_matches_module_version(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "app.py"
        result = subprocess.run([sys.executable, str(launcher), "--version"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"SynAI {__version__}\n")

    def test_existing_arguments_and_connection_resolution_preserved(self) -> None:
        with patch("synai.preferences.PreferencesStore") as store, \
             patch("synai.tui.application.CodingApp") as app:
            store.return_value.load.return_value = Preferences()
            main(["--ollama-url", "http://trusted:11434", "--request-timeout", "90",
                  "--runtime", "podman", "--image", "trusted:local",
                  "--command-timeout", "30", "--output-bytes", "8192", "--tool-budget", "5"])
            settings = app.call_args.args[0]
            self.assertEqual(settings.ollama_url, "http://trusted:11434")
            self.assertEqual(settings.request_timeout, 90)
            self.assertEqual(settings.runtime, "podman")
            self.assertEqual(settings.image, "trusted:local")
            self.assertEqual((settings.command_timeout, settings.output_bytes, settings.tool_budget), (30, 8192, 5))
            app.return_value.run.assert_called_once_with()

    def test_retired_history_option_and_invalid_settings_remain_errors(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            main(["--history-dir", "/tmp/not-used"])
        self.assertEqual(exit.exception.code, 2)
        with patch("synai.preferences.PreferencesStore") as store, \
             patch("synai.tui.application.CodingApp") as app, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            store.return_value.load.return_value = Preferences()
            main(["--tool-budget", "0"])
        self.assertEqual(exit.exception.code, 2)
        app.assert_not_called()

    def test_source_guards_cover_package_and_checkout_without_all_site_packages(self) -> None:
        for path in (SOURCE, SOURCE / "nested", SOURCE.parent):
            self.assertTrue(source_overlap(path))
        if CHECKOUT is not None:
            self.assertTrue(source_overlap(CHECKOUT / "tests"))
            with self.assertRaisesRegex(ValueError, "overlap"):
                ConversationStorage(CHECKOUT / ".synai")
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(source_overlap(Path(directory) / "unrelated"))


if __name__ == "__main__":
    unittest.main()
