from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from synai.config import Settings
from synai.models import Session
from synai.storage import ConversationStorage
from synai.tools import Tools, schemas


class GitBackend:
    def __init__(
        self,
        workspace: Path,
        *,
        timeout: float = 5,
        history_dir: Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.settings = Settings(
            execution_mode="host",
            history_dir=history_dir or workspace.parent / "synai-private",
            command_timeout=timeout,
        )
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.missing_git = False
        self.output_transform = None

    @property
    def execution_workspace(self) -> Path:
        if self.settings.execution_mode == "sandbox":
            return getattr(self, "mapped_workspace", Path("/workspace"))
        return self.workspace

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
                timeout=self.settings.command_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
            stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            return {
                "ok": False,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "stderr_base64": base64.b64encode(stderr).decode("ascii"),
                "exit_code": -9,
                "truncated": False,
                "timed_out": True,
            }
        stdout = result.stdout
        stderr = result.stderr
        output = {
            "ok": result.returncode == 0,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stdout_base64": base64.b64encode(stdout).decode("ascii"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "stderr_base64": base64.b64encode(stderr).decode("ascii"),
            "exit_code": result.returncode,
            "truncated": False,
            "timed_out": False,
        }
        if self.settings.execution_mode == "sandbox" and "rev-parse" in str(arguments["command"]):
            reported = Path(output["stdout"].strip())
            try:
                relative = reported.relative_to(self.workspace)
                output["stdout"] = (Path("/workspace") / relative).as_posix() + "\n"
            except ValueError:
                output["stdout"] = (Path("/outside") / reported.name).as_posix() + "\n"
            raw = output["stdout"].encode()
            output["stdout_base64"] = base64.b64encode(raw).decode("ascii")
        if self.output_transform is not None:
            output = self.output_transform(str(arguments["command"]), output)
        return output


class SandboxMappedGitBackend(GitBackend):
    def __init__(self, workspace: Path, history_dir: Path) -> None:
        super().__init__(workspace, history_dir=history_dir)
        self.settings = replace(self.settings, execution_mode="sandbox")
        self.mapped_workspace = Path("/workspace")


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

    def create_sandbox_workspace(self) -> tuple[Path, Path, str]:
        history = Path(self.temp.name) / "managed-history"
        storage = ConversationStorage(history)
        session_id = "e" * 32
        storage.create(session_id, workspace=True)
        return storage.workspace(session_id), history, session_id

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

    async def test_sandbox_workspace_mapping_uses_container_root_and_relative_paths(self) -> None:
        workspace, history, session_id = self.create_sandbox_workspace()
        self.workspace = workspace
        self.backend.workspace = workspace
        self.initialize_repo()
        (workspace / "client.py").write_text("changed\n")
        backend = SandboxMappedGitBackend(workspace, history)
        session = replace(self.session, workspace=str(workspace), session_id=session_id)
        result = await Tools(backend, AsyncMock(return_value=True)).call(
            "git_status", {}, session=session,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["repository_identity"]["root"], ".")
        self.assertEqual(result["workspace_identity"], str(self.workspace))
        self.assertIn("client.py", {item["source_path"] for item in result["records"]})
        self.assertTrue(all("/workspace" not in item["source_path"] for item in result["records"]))
        commands = [str(arguments["command"]) for _, arguments in backend.calls]
        status_commands = [command for command in commands if "status --porcelain" in command]
        self.assertEqual(len(status_commands), 1)
        self.assertIn("-C .", status_commands[0])

    async def test_sandbox_mapping_supports_selected_nested_repository_and_rejects_parent(self) -> None:
        base, history, session_id = self.create_sandbox_workspace()
        self.workspace = base
        self.backend.workspace = base
        git(history, "init", "-q")
        self.initialize_repo()
        backend = SandboxMappedGitBackend(base, history)
        session = replace(self.session, workspace=str(base), session_id=session_id)
        result = await Tools(backend, AsyncMock(return_value=True)).call(
            "git_status", {}, session=session,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["repository_identity"]["root"], ".")

        outside = SandboxMappedGitBackend(base, history)
        outside_session = replace(self.session, workspace=str(base), session_id=session_id)

        def report_outside(command: str, output: dict[str, object]) -> dict[str, object]:
            if "rev-parse --show-toplevel" in command:
                output["stdout"] = "/outside/repository\n"
                output["stdout_base64"] = base64.b64encode(b"/outside/repository\n").decode("ascii")
            return output

        outside.output_transform = report_outside
        rejected = await Tools(outside, AsyncMock(return_value=True)).call(
            "git_status", {}, session=outside_session,
        )
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["error_code"], "UNSAFE_PATH")

    async def test_sandbox_backend_mapping_is_checked_before_and_after_discovery(self) -> None:
        workspace, history, session_id = self.create_sandbox_workspace()
        self.workspace = workspace
        self.backend.workspace = workspace
        self.initialize_repo()
        backend = SandboxMappedGitBackend(workspace, history)
        session = replace(self.session, workspace=str(workspace), session_id=session_id)
        backend.mapped_workspace = Path("/wrong")
        wrong = await Tools(backend, AsyncMock(return_value=True)).call(
            "git_status", {}, session=session,
        )
        self.assertEqual(wrong["error_code"], "WORKSPACE_CHANGED")
        self.assertEqual(backend.calls, [])

        backend.mapped_workspace = Path("/workspace")

        def change_mapping(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                backend.mapped_workspace = Path("/changed")
            return result

        backend.output_transform = change_mapping
        changed = await Tools(backend, AsyncMock(return_value=True)).call(
            "git_status", {}, session=session,
        )
        self.assertEqual(changed["error_code"], "WORKSPACE_CHANGED")
        self.assertFalse(changed["success"])

    async def test_sandbox_non_git_and_missing_git_are_reported(self) -> None:
        workspace, history, session_id = self.create_sandbox_workspace()
        backend = SandboxMappedGitBackend(workspace, history)
        session = replace(self.session, workspace=str(workspace), session_id=session_id)
        not_git = await Tools(backend, AsyncMock(return_value=True)).call(
            "git_status", {}, session=session,
        )
        self.assertFalse(not_git["success"])
        self.assertEqual(not_git["error_code"], "NOT_GIT_REPOSITORY")
        self.assertEqual(not_git["outcome"], "UNAVAILABLE")

        backend.missing_git = True
        unavailable = await Tools(backend, AsyncMock(return_value=True)).call(
            "git_status", {}, session=session,
        )
        self.assertEqual(unavailable["error_code"], "GIT_UNAVAILABLE")

    async def test_git_reported_traversal_and_symlink_escape_are_rejected(self) -> None:
        self.initialize_repo()
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("outside\n")
        (self.workspace / "escape.py").symlink_to(outside)

        status = await self.call("git_status", {})
        self.assertFalse(status["success"])
        self.assertEqual(status["error_code"], "UNSAFE_PATH")

        def traversal(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                raw = b"? ../outside.py\0"
                result["stdout"] = raw.decode()
                result["stdout_base64"] = base64.b64encode(raw).decode("ascii")
            return result

        self.backend.output_transform = traversal
        self.workspace.joinpath("escape.py").unlink()
        status = await self.call("git_status", {})
        self.assertFalse(status["success"])
        self.assertEqual(status["error_code"], "UNSAFE_PATH")

    async def test_status_preserves_lossless_unusual_filename_bytes(self) -> None:
        self.initialize_repo()
        names = [
            "with spaces.py",
            'quote"name.py',
            "unicode-\u2603.py",
            "with\ttab.py",
            "with\nnewline.py",
            "-leading-dash.py",
            os.fsdecode(b"invalid-\xff-name.py"),
        ]
        for name in names:
            (self.workspace / name).write_text("new\n")

        result = await self.call("git_status", {})

        self.assertTrue(result["success"], result)
        paths = {item["source_path"] for item in result["records"]}
        self.assertEqual(paths, set(names))
        self.assertIn("\udcff", next(path for path in paths if "invalid-" in path))

    async def test_incomplete_git_status_is_never_authoritative(self) -> None:
        self.initialize_repo()

        def partial(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                raw = b"? complete.py\0? unfinished"
                result.update({
                    "ok": False,
                    "stdout": raw.decode(),
                    "stdout_base64": base64.b64encode(raw).decode("ascii"),
                    "exit_code": -9,
                    "truncated": True,
                    "terminated_by_output_limit": True,
                })
            return result

        self.backend.output_transform = partial
        result = await self.call("git_status", {})

        self.assertEqual(result["outcome"], "PARTIAL")
        self.assertFalse(result["success"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["authoritative"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["exit_code"], -9)
        self.assertEqual([item["source_path"] for item in result["records"]], ["complete.py"])

    async def test_successful_but_truncated_status_is_partial_and_non_authoritative(self) -> None:
        self.initialize_repo()

        def truncated_after_success(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                raw = b"? first.py\0"
                result.update({
                    "stdout": raw.decode(),
                    "stdout_base64": base64.b64encode(raw).decode("ascii"),
                    "truncated": True,
                    "exit_code": 0,
                    "ok": True,
                })
            return result

        self.backend.output_transform = truncated_after_success
        result = await self.call("git_status", {})

        self.assertEqual(result["outcome"], "PARTIAL")
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["process_completed"])
        self.assertFalse(result["authoritative"])

    async def test_cancelled_missing_exit_and_malformed_git_outputs_are_not_authoritative(self) -> None:
        self.initialize_repo()

        def cancelled(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                result.update({"ok": False, "cancelled": True})
            return result

        self.backend.output_transform = cancelled
        result = await self.call("git_status", {})
        self.assertEqual(result["outcome"], "CANCELLED")
        self.assertTrue(result["cancelled"])

        def missing_exit(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                result.pop("exit_code", None)
            return result

        self.backend.output_transform = missing_exit
        result = await self.call("git_status", {})
        self.assertEqual(result["outcome"], "FAILED")
        self.assertEqual(result["error_code"], "GIT_OPERATION_FAILED")
        self.assertIsNone(result["exit_code"])

        def malformed(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                raw = b"not-a-status-record\0"
                result["stdout"] = raw.decode()
                result["stdout_base64"] = base64.b64encode(raw).decode("ascii")
            return result

        self.backend.output_transform = malformed
        result = await self.call("git_status", {})
        self.assertEqual(result["outcome"], "PARTIAL")
        self.assertFalse(result["authoritative"])

    async def test_large_git_diff_patch_is_reported_partial(self) -> None:
        self.initialize_repo()
        path = self.workspace / "large.txt"
        path.write_text("before\n", encoding="utf-8")
        git(self.workspace, "add", "large.txt")
        git(self.workspace, "commit", "-qm", "add large file")
        path.write_text("".join(f"line {number}\n" for number in range(12_000)), encoding="utf-8")

        result = await self.call("git_diff", {
            "mode": "unstaged",
            "revision": "",
            "paths": ["large.txt"],
            "include_patch": True,
        })

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "PARTIAL")
        self.assertTrue(result["truncated"])
        self.assertFalse(result["authoritative"])
        self.assertEqual(len(result["records"][0]["patch"]), 64 * 1024)

    async def test_nonzero_truncated_git_command_is_failed_not_successful(self) -> None:
        self.initialize_repo()

        def failed(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                result.update({
                    "ok": False, "exit_code": 1, "truncated": True,
                    "terminated_by_output_limit": False,
                })
            return result

        self.backend.output_transform = failed
        result = await self.call("git_status", {})

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "FAILED")
        self.assertEqual(result["error_code"], "GIT_OPERATION_FAILED")

    async def test_lossy_git_text_without_byte_representation_is_incomplete(self) -> None:
        self.initialize_repo()

        def lossy(command: str, result: dict[str, object]) -> dict[str, object]:
            if "status --porcelain" in command:
                result["stdout"] = "? damaged-\ufffd.py\0"
                result.pop("stdout_base64", None)
            return result

        self.backend.output_transform = lossy
        result = await self.call("git_status", {})

        self.assertEqual(result["outcome"], "PARTIAL")
        self.assertFalse(result["authoritative"])
        self.assertEqual(result["records"], [])

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
