from __future__ import annotations

import json
import unittest
from datetime import datetime

from synai.models import Activity
from synai.tui.activity import TITLES, format_activity


def record(kind: str, name: str, data: object) -> Activity:
    return Activity(kind, f"{name}: {json.dumps(data)}", "2026-10-03T05:00:00+00:00")


class ActivityTests(unittest.TestCase):
    def test_requests_have_readable_targets_and_bounded_previews(self) -> None:
        cases = {
            "list_files": ({"path": "src"}, ["Directory: src"]),
            "read_file": ({"path": "main.py"}, ["File: main.py"]),
            "write_file": ({"path": "main.py", "content": "print('hi')\n"}, ["File: main.py", "Proposed content (1 lines, 12 UTF-8 bytes)", "print('hi')"]),
            "patch_file": ({"path": "main.py", "old": "bad", "new": "good"}, ["Replace", "bad", "With", "good"]),
            "delete_file": ({"path": "old.py"}, ["File: old.py"]),
            "terminal": ({"command": "python -m unittest", "cwd": "."}, ["Command: python -m unittest", "Working directory: ."]),
            "fetch_url": ({"url": "https://example.com"}, ["URL: https://example.com"]),
        }
        for name, (data, expected) in cases.items():
            with self.subTest(name=name):
                entry = record("tool", name, data)
                before = entry.text
                text = format_activity(entry).plain
                self.assertIn(TITLES[name] + " // Requested", text)
                for value in expected:
                    self.assertIn(value, text)
                self.assertEqual(entry.text, before)
        text = format_activity(record("tool", "write_file", {"path": "x", "content": "a" * 50000})).plain
        self.assertIn("File: x", text)
        self.assertIn("Preview shortened", text)
        self.assertLess(len(text), 4000)

    def test_terminal_output_preserves_multiline_and_metrics(self) -> None:
        text = format_activity(record("result", "terminal", {
            "ok": False, "exit_code": 1, "duration": 1.234,
            "stdout": "first\nsecond\n", "stderr": "Traceback\nfailed\n",
            "timed_out": False, "truncated": False,
        })).plain
        for value in ("Failed", "Exit code: 1", "Elapsed: 1.23s", "STDOUT\nfirst\nsecond\n",
                      "STDERR\nTraceback\nfailed\n"):
            self.assertIn(value, text)
        empty = format_activity(record("result", "terminal", {"ok": True, "stdout": "", "stderr": ""})).plain
        self.assertEqual(empty.count("(no output)"), 2)

    def test_outcomes_are_distinct_and_not_success_shaped(self) -> None:
        cases = [
            ({"ok": False, "denied": True, "error": "User denied"}, "Denied by you - not executed"),
            ({"ok": False, "timed_out": True}, "Timed out"),
            ({"ok": False, "truncated": True}, "Stopped at output limit"),
            ({"ok": False, "error": "Interrupted; action not replayed"}, "Interrupted - not replayed"),
            ({"error": "missing success"}, "Unknown result"),
            ({"ok": "yes"}, "Unknown result"),
            ({"ok": True, "timed_out": "false"}, "Unknown result"),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                text = format_activity(record("result", "terminal", data)).plain
                self.assertIn(expected, text)
                self.assertNotIn("Succeeded", text)

    def test_read_listing_mutation_and_http_results(self) -> None:
        cases = [
            ("read_file", {"ok": True, "content": "a\nb\n", "sha256": "privatehash"}, ["File content (2 lines, 4 UTF-8 bytes)", "a\nb\n"]),
            ("list_files", {"ok": True, "entries": [{"name": "src", "directory": True}, {"name": "link", "symlink": True}, {"name": "main.py"}]},
             ["Directory entries (3)", "src/", "link [symlink]", "main.py"]),
            ("write_file", {"ok": True, "path": "new.py"}, ["File: new.py"]),
            ("patch_file", {"ok": True, "path": "new.py"}, ["File: new.py"]),
            ("delete_file", {"ok": True, "path": "old.py"}, ["File: old.py"]),
            ("fetch_url", {"ok": True, "status": 200, "text": "body", "truncated": True},
             ["HTTP status: 200", "Response", "body", "Output was truncated"]),
        ]
        for name, data, expected in cases:
            with self.subTest(name=name):
                text = format_activity(record("result", name, data)).plain
                self.assertIn("Succeeded", text)
                for value in expected:
                    self.assertIn(value, text)
                self.assertNotIn("privatehash", text)

    def test_legacy_invalid_and_unknown_records_remain_visible(self) -> None:
        for content in ("terminal: {broken", "other_tool: {}", "terminal: []", "legacy text"):
            with self.subTest(content=content):
                text = format_activity(Activity("result", content)).plain
                self.assertIn("Unformatted tool activity", text)
                self.assertIn(content, text)
        invalid = format_activity(record("result", "terminal", {
            "ok": True, "duration": -1, "exit_code": False, "stderr": 9, "new_field": "more",
        })).plain
        self.assertIn("invalid duration", invalid)
        self.assertIn("Exit code: unavailable or invalid", invalid)
        self.assertIn("Invalid output field", invalid)
        self.assertIn("Additional result fields", invalid)
        self.assertIn("more", invalid)
        self.assertIn("Invalid directory listing", format_activity(record("result", "list_files", {"ok": True, "entries": {}})).plain)
        self.assertIn("Invalid directory entry", format_activity(record("result", "list_files", {"ok": True, "entries": [None]})).plain)
        self.assertIn("Invalid or missing text field", format_activity(record("result", "read_file", {"ok": True, "content": None})).plain)
        self.assertIn("HTTP status: invalid", format_activity(record("result", "fetch_url", {"ok": True, "status": "two hundred"})).plain)

    def test_clock_semantic_headings_literal_markup_and_large_results(self) -> None:
        entry = record("result", "terminal", {"ok": False, "error": "[bold]not markup[/bold]", "stdout": "x" * 10000})
        before = entry.__dict__.copy()
        text = format_activity(entry)
        clock = datetime.fromisoformat(entry.created_at).astimezone().strftime("%H:%M:%S")
        self.assertTrue(text.plain.startswith(clock))
        self.assertIn("[bold]not markup[/bold]", text.plain)
        self.assertIn("Preview shortened", text.plain)
        self.assertLess(len(text), 5000)
        self.assertEqual(entry.__dict__, before)
        for kind in ("approval", "limit", "cancel", "resume", "error"):
            self.assertNotIn(f"[{kind}]", format_activity(Activity(kind, "Some event")).plain)
        self.assertIn("Allowed: Write file", format_activity(Activity("approval", "Allowed: write_file")).plain)
        self.assertIn("Invalid timestamp", format_activity(Activity("error", "Oops", "bad")).plain)
        self.assertIn("Invalid timestamp", format_activity(Activity("error", "Oops", "2026-10-03T05:00:00")).plain)
        self.assertIn("invalid duration", format_activity(record("result", "terminal", {"ok": True, "duration": 10 ** 400})).plain)
        self.assertIn("Unformatted activity", format_activity(Activity("unknown", "Still visible")).plain)
