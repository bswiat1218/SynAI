from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from synai.config import Settings
from synai.models import Session
from synai.tools import Tools, schemas


class GitBackend:
    def __init__(self, workspace: Path, *, timeout: float = 5) -> None:
        self.workspace = workspace.resolve()
        self.settings = Settings(
            execution_mode="host",
            history_dir=workspace.parent / "synai-private",
            command_timeout=timeout,
        )
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.missing_git = False

    def matches(self, session: Session) -> bool:
        return Path(session.workspace) == self.workspace

    async def execute(
        self,
        name: str,
        arguments: dict[str, object],
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        del expected_sha256
        self.calls.append((name, dict(arguments)))
        if name != "terminal":
            return {"ok": False, "error": "Unexpected backend operation"}
        if self.missing_git:
            return {
                "ok": False, "stdout": "", "stderr": "/bin/sh: git: not found",
                "exit_code": 127, "truncated": False, "timed_out": False,
            }
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                str(arguments["command"]),
                shell=True,
                cwd=self.workspace,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.settings.command_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "stdout": str(exc.stdout or ""),
                "stderr": str(exc.stderr or ""),
                "exit_code": -9,
                "truncated": False,
                "timed_out": True,
            }
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "truncated": False,
            "timed_out": False,
        }


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class GitToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.backend = GitBackend(self.workspace)
        self.session = Session(
            "test", "http://localhost:11434", str(self.workspace),
            session_id="a" * 32,
        )
        self.approve = AsyncMock(return_value=True)
        self.tools = Tools(self.backend, self.approve)

    def initialize_repo(self) -> str:
        git(self.workspace, "init", "-q")
        git(self.workspace, "config", "user.name", "Test User")
        git(self.workspace, "config", "user.email", "test@example.invalid")
        (self.workspace / "client.py").write_text("before = 1\n", encoding="utf-8")
        git(self.workspace, "add", "client.py")
        git(self.workspace, "commit", "-qm", "initial")
        return git(self.workspace, "rev-parse", "HEAD")

    async def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return await self.tools.call(name, arguments, session=self.session)

    async def test_status_preserves_staged_unstaged_deleted_and_untracked(self) -> None:
        self.initialize_repo()
        (self.workspace / "gone.py").write_text("remove\n", encoding="utf-8")
        git(self.workspace, "add", "gone.py")
        git(self.workspace, "commit", "-qm", "add gone")
        (self.workspace / "gone.py").unlink()
        (self.workspace / "client.py").write_text("staged = 1\n", encoding="utf-8")
        git(self.workspace, "add", "client.py")
        (self.workspace / "client.py").write_text("unstaged = 1\n", encoding="utf-8")
        (self.workspace / "new ; file.py").write_text("new = 1\n", encoding="utf-8")

        result = await self.call("git_status", {})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["operation"], "git_status")
        records = result["records"]
        by_path = {item["source_path"]: item for item in records}
        self.assertTrue(by_path["client.py"]["staged"], records)
        self.assertTrue(by_path["client.py"]["unstaged"])
        self.assertEqual(by_path["gone.py"]["category"], "deleted")
        self.assertEqual(by_path["new ; file.py"]["category"], "untracked")
        unusual = self.workspace / "line\nbreak ; file.py"
        unusual.write_text("unusual = True\n", encoding="utf-8")
        status = await self.call("git_status", {})
        unusual_records = {
            item["source_path"]: item for item in status["records"]
        }
        self.assertIn("line\nbreak ; file.py", unusual_records)
        self.assertEqual(self.approve.await_count, 2)

    async def test_status_reports_renames_and_merge_conflicts(self) -> None:
        self.initialize_repo()
        git(self.workspace, "mv", "client.py", "renamed.py")
        renamed = await self.call("git_status", {})
        rename_record = next(
            item for item in renamed["records"]
            if item["category"] == "renamed"
        )
        self.assertEqual(rename_record["source_path"], "client.py")
        self.assertEqual(rename_record["destination_path"], "renamed.py")
        git(self.workspace, "add", "-A")
        git(self.workspace, "commit", "-qm", "rename")

        branch = git(self.workspace, "branch", "--show-current")
        git(self.workspace, "checkout", "-qb", "phase9-conflict")
        (self.workspace / "renamed.py").write_text("side = 1\n", encoding="utf-8")
        git(self.workspace, "commit", "-qam", "side change")
        git(self.workspace, "checkout", "-q", branch)
        (self.workspace / "renamed.py").write_text("main = 1\n", encoding="utf-8")
        git(self.workspace, "commit", "-qam", "main change")
        conflict = subprocess.run(
            ["git", "merge", "phase9-conflict"],
            cwd=self.workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(conflict.returncode, 0)

        status = await self.call("git_status", {})

        self.assertTrue(status["success"], status)
        self.assertTrue(any(item["category"] == "conflict" for item in status["records"]))

    async def test_binary_diff_is_metadata_only_and_timeout_is_reported(self) -> None:
        self.initialize_repo()
        binary = self.workspace / "image.bin"
        binary.write_bytes(b"\x00\x01base")
        git(self.workspace, "add", "image.bin")
        git(self.workspace, "commit", "-qm", "add binary")
        binary.write_bytes(b"\x00\x02updated")
        diff = await self.call("git_diff", {
            "mode": "unstaged",
            "revision": "",
            "paths": ["image.bin"],
            "include_patch": True,
        })
        self.assertTrue(diff["success"], diff)
        self.assertTrue(diff["records"][0]["binary"])
        self.assertIsNone(diff["records"][0]["patch"])

        self.backend.settings = replace(
            self.backend.settings,
            command_timeout=0.000001,
        )
        timed_out = await self.call("git_status", {})
        self.assertFalse(timed_out["success"])
        self.assertEqual(timed_out["error_code"], "GIT_OPERATION_FAILED")

    async def test_diff_log_and_show_are_bounded_and_revision_checked(self) -> None:
        commit = self.initialize_repo()
        (self.workspace / "client.py").write_text("after = 2\n", encoding="utf-8")
        diff = await self.call("git_diff", {
            "mode": "unstaged", "revision": "", "paths": ["client.py"],
            "include_patch": True,
        })
        self.assertTrue(diff["success"], diff)
        self.assertEqual(diff["records"][0]["additions"], 1)
        self.assertIn("+after", diff["records"][0]["patch"])

        history = await self.call("git_log", {"limit": 1})
        self.assertTrue(history["success"])
        self.assertEqual(history["result_count"], 1)
        self.assertEqual(history["records"][0]["commit"], commit)

        shown = await self.call("git_show", {"revision": commit[:12], "path": "client.py"})
        self.assertTrue(shown["success"])
        self.assertIn("before = 1", shown["records"][0]["content"])

        invalid = await self.call("git_show", {"revision": "HEAD;touch", "path": ""})
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error_code"], "INVALID_ARGUMENTS")

    async def test_revision_and_path_injection_are_rejected_before_execution(self) -> None:
        self.initialize_repo()
        count = len(self.backend.calls)

        bad_revision = await self.call("git_diff", {
            "mode": "revision", "revision": "--output=/tmp/x",
            "paths": [], "include_patch": False,
        })
        bad_path = await self.call("git_diff", {
            "mode": "unstaged", "revision": "", "paths": ["../outside"],
            "include_patch": False,
        })

        self.assertFalse(bad_revision["ok"])
        self.assertFalse(bad_path["ok"])
        self.assertEqual(len(self.backend.calls), count)
        self.assertEqual(len(schemas()), len({tool["function"]["name"] for tool in schemas()}))

    async def test_parent_repository_and_git_metadata_escape_are_unsupported(self) -> None:
        git(self.workspace, "init", "-q")
        nested = self.workspace / "nested"
        nested.mkdir()
        backend = GitBackend(nested)
        tools = Tools(backend, AsyncMock(return_value=True))
        session = Session(
            "test", "http://localhost:11434", str(nested),
            session_id="b" * 32,
        )

        result = await tools.call("git_status", {}, session=session)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "UNSAFE_PATH", result)
        self.assertIn("outside", result["error"])

    async def test_worktree_within_workspace_is_supported(self) -> None:
        commit = self.initialize_repo()
        worktree = self.workspace / "worktree"
        git(self.workspace, "worktree", "add", "-q", "-b", "test-branch", str(worktree), commit)
        backend = GitBackend(self.workspace)
        session = Session(
            "test", "http://localhost:11434", str(self.workspace),
            session_id="c" * 32,
        )
        result = await Tools(backend, AsyncMock(return_value=True)).call(
            "git_status", {}, session=session,
        )
        self.assertTrue(result["success"], result)

    async def test_missing_executable_is_reported_separately(self) -> None:
        self.initialize_repo()
        self.backend.missing_git = True

        result = await self.call("git_status", {})

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "GIT_UNAVAILABLE")

    async def test_checkpoint_does_not_run_unapproved_git_commands(self) -> None:
        self.initialize_repo()
        result = await self.call("git_checkpoint", {
            "task_id": "",
            "paths": ["client.py"],
            "require_complete": True,
        })

        self.assertTrue(result["success"], result)
        self.assertIsNone(result["repository_identity"])
        self.assertTrue(any(
            "Repository identity unavailable" in item
            for item in result["limitations"]
        ))
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
