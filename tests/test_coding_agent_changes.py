from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from synai.coding_agent.changes import BaselineCaptureError, TaskChangeTracker
from synai.config import Settings
from synai.intelligence import RepositoryIndex


@dataclass
class ChangeTask:
    task_id: str = "task-change-test"
    change_baselines: list[object] = field(default_factory=list)
    change_evidence: list[object] = field(default_factory=list)


class TaskChangeTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.settings = Settings(
            execution_mode="host",
            history_dir=Path(self.temp.name) / "history",
        )
        self.repository = RepositoryIndex(self.root)
        self.task = ChangeTask()
        self.tracker = TaskChangeTracker(
            self.task, self.repository, self.settings, str(self.root),
        )

    def mutate(
        self,
        execution_id: str,
        tool_name: str,
        arguments: dict[str, str],
        *,
        repair_attempt_id: int | None = None,
    ) -> dict[str, object]:
        path = arguments["path"]
        self.tracker.before_mutation(
            execution_id,
            path,
            "modify",
            repair_attempt_id=repair_attempt_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        target = self.root / path
        if tool_name == "write_file":
            target.write_text(arguments["content"], encoding="utf-8")
        elif tool_name == "patch_file":
            current = target.read_text(encoding="utf-8")
            target.write_text(
                current.replace(arguments["old"], arguments["new"], 1),
                encoding="utf-8",
            )
        else:
            raise AssertionError(tool_name)
        return self.tracker.after_mutation(execution_id, {"ok": True})

    def test_dirty_baseline_survives_repeated_implementation_and_repair(self) -> None:
        path = self.root / "client.py"
        path.write_text("user = 1\n", encoding="utf-8")
        baseline_digest = hashlib.sha256(path.read_bytes()).hexdigest()

        implementation = self.mutate(
            "exec-1", "write_file",
            {"path": "client.py", "content": "agent = 2\n"},
        )
        repair = self.mutate(
            "exec-2", "patch_file",
            {"path": "client.py", "old": "agent = 2", "new": "agent = 3"},
            repair_attempt_id=1,
        )

        self.assertEqual(self.task.change_baselines[0].sha256, baseline_digest)
        self.assertEqual([item.outcome for item in self.task.change_evidence], ["succeeded", "succeeded"])
        self.assertIsNone(self.task.change_evidence[0].repair_attempt_id)
        self.assertEqual(self.task.change_evidence[1].repair_attempt_id, 1)
        self.assertIn("-user = 1", self.task.change_evidence[0].diff)
        self.assertEqual(implementation["change_evidence"]["outcome"], "succeeded")
        self.assertEqual(repair["change_evidence"]["outcome"], "succeeded")

    def test_noop_and_external_postimage_are_not_claimed_as_agent_changes(self) -> None:
        path = self.root / "client.py"
        path.write_text("same\n", encoding="utf-8")
        noop = self.mutate(
            "exec-noop", "write_file",
            {"path": "client.py", "content": "same\n"},
        )
        self.assertEqual(noop["change_evidence"]["outcome"], "no_op")
        self.assertFalse(noop["mutation_changed"])

        self.tracker.before_mutation(
            "exec-external", "client.py", "modify", repair_attempt_id=None,
            tool_name="write_file",
            arguments={"path": "client.py", "content": "requested\n"},
        )
        path.write_text("external\n", encoding="utf-8")
        external = self.tracker.after_mutation("exec-external", {"ok": True})

        self.assertEqual(external["change_evidence"]["outcome"], "uncertain")
        self.assertTrue(external["change_evidence"]["uncertain"])
        self.assertFalse(external["change_attribution_complete"])

    def test_failed_first_attempt_does_not_hide_later_external_change(self) -> None:
        path = self.root / "client.py"
        path.write_text("baseline\n", encoding="utf-8")
        self.tracker.before_mutation(
            "exec-failed", "client.py", "modify", repair_attempt_id=None,
            tool_name="write_file",
            arguments={"path": "client.py", "content": "agent result\n"},
        )
        failed = self.tracker.after_mutation("exec-failed", {"ok": False})
        self.assertEqual(failed["change_evidence"]["outcome"], "failed")

        path.write_text("external change\n", encoding="utf-8")
        self.tracker.before_mutation(
            "exec-retry", "client.py", "modify", repair_attempt_id=None,
            tool_name="write_file",
            arguments={"path": "client.py", "content": "agent result\n"},
        )
        path.write_text("agent result\n", encoding="utf-8")
        retried = self.tracker.after_mutation("exec-retry", {"ok": True})

        self.assertEqual(retried["change_evidence"]["outcome"], "uncertain")
        self.assertTrue(retried["change_evidence"]["uncertain"])
        self.assertIn("external change", retried["change_evidence"]["diff"])
        self.assertEqual(self.task.change_baselines[0].sha256, hashlib.sha256(b"baseline\n").hexdigest())

    def test_oversized_baseline_blocks_mutation_before_dispatch(self) -> None:
        (self.root / "large.py").write_bytes(b"x" * 1_048_577)
        with self.assertRaises(BaselineCaptureError):
            self.tracker.before_mutation(
                "exec-large", "large.py", "modify", repair_attempt_id=None,
                tool_name="write_file",
                arguments={"path": "large.py", "content": "replacement\n"},
            )
