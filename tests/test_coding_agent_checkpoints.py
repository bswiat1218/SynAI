from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

from synai.coding_agent.checkpoints import CheckpointManager
from synai.config import Settings
from synai.intelligence import IndexLimits, RepositoryIndex
from synai.models import Session
from synai.tools import Tools, schemas


class CheckpointBackend:
    def __init__(self, workspace: Path, history_dir: Path) -> None:
        self.workspace = workspace.resolve()
        self.settings = Settings(
            execution_mode="host",
            history_dir=history_dir,
            output_bytes=1_048_576,
        )
        self.calls: list[str] = []

    def matches(self, session: Session) -> bool:
        return Path(session.workspace) == self.workspace

    async def execute(
        self,
        name: str,
        arguments: dict[str, object],
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(name)
        target = self.workspace / str(arguments.get("path", ""))
        if name == "preview":
            content = target.read_text(encoding="utf-8") if target.exists() else None
            digest = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
            return {"ok": True, "content": content, "sha256": digest}
        current = target.read_text(encoding="utf-8") if target.exists() else None
        digest = hashlib.sha256(current.encode()).hexdigest() if current is not None else None
        if digest != expected_sha256:
            return {"ok": False, "error_code": "WORKSPACE_CHANGED"}
        if name == "write_file":
            target.write_text(str(arguments["content"]), encoding="utf-8")
        elif name == "delete_file":
            target.unlink()
        else:
            return {"ok": False, "error": "Unsupported test operation"}
        return {"ok": True, "path": arguments["path"]}


class CheckpointToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.history = base / "private-history"
        self.backend = CheckpointBackend(self.workspace, self.history)
        self.session = Session(
            "test", "http://localhost:11434", str(self.workspace),
            session_id="b" * 32,
        )
        self.approve = AsyncMock(return_value=True)
        self.tools = Tools(self.backend, self.approve)
        self.index = RepositoryIndex(self.workspace, IndexLimits())

    async def checkpoint(self, path: str = "client.py") -> str:
        result = await self.tools.call(
            "git_checkpoint",
            {
                "task_id": "task-1",
                "paths": [path],
                "require_complete": True,
            },
            session=self.session,
        )
        self.assertTrue(result["success"], result)
        return str(result["checkpoint_id"])

    async def test_chat_schemas_unchanged_unless_phase_nine_tools_requested(self) -> None:
        default_names = {tool["function"]["name"] for tool in schemas()}
        self.assertNotIn("git_status", default_names)
        self.assertNotIn("git_checkpoint", default_names)
        phase_nine_names = {
            tool["function"]["name"]
            for tool in schemas(include_git=True, include_checkpoints=True)
        }
        self.assertIn("git_status", phase_nine_names)
        self.assertIn("git_checkpoint", phase_nine_names)
        self.assertIn("restore_checkpoint", phase_nine_names)

    async def test_approved_restore_preserves_dirty_baseline(self) -> None:
        (self.workspace / "client.py").write_text("user = 'dirty'\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        (self.workspace / "client.py").write_text("agent = 'change'\n", encoding="utf-8")
        manager = CheckpointManager(self.backend.settings)
        manager.update_task_postimage(
            "task-1", str(self.workspace), "client.py", True,
            hashlib.sha256(b"agent = 'change'\n").hexdigest(),
        )

        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["records"], [{"path": "client.py", "outcome": "restored"}])
        self.assertEqual((self.workspace / "client.py").read_text(), "user = 'dirty'\n")
        self.assertEqual(self.approve.await_count, 3)

    async def test_checkpoint_with_optional_repository_identity_remains_workspace_bound(self) -> None:
        target = self.workspace / "client.py"
        target.write_text("dirty baseline\n", encoding="utf-8")
        manager = CheckpointManager(self.backend.settings)
        created = manager.create(
            self.session,
            self.index,
            ["client.py"],
            task_id="repository-provenance",
            repository_identity={"root": ".", "git_directory": ".git"},
        )
        self.assertTrue(created["success"], created)
        target.write_text("agent content\n", encoding="utf-8")
        manager.update_task_postimage(
            "repository-provenance",
            str(self.workspace),
            "client.py",
            True,
            hashlib.sha256(b"agent content\n").hexdigest(),
        )

        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": created["checkpoint_id"], "paths": ["client.py"]},
            session=self.session,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(target.read_text(), "dirty baseline\n")
        self.assertNotIn("terminal", self.backend.calls)

    async def test_legacy_checkpoint_without_repository_identity_remains_readable(self) -> None:
        target = self.workspace / "client.py"
        target.write_text("legacy snapshot\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        metadata_path = self.history / "checkpoints" / f"{checkpoint_id}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        legacy_payload = dict(metadata)
        legacy_payload.pop("integrity_sha256")
        legacy_payload.pop("repository_identity")
        metadata.pop("repository_identity")
        metadata["integrity_sha256"] = hashlib.sha256(json.dumps(
            legacy_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["records"], [{"path": "client.py", "outcome": "unchanged"}])

    async def test_repository_metadata_change_during_approval_fails_checkpoint_revalidation(self) -> None:
        target = self.workspace / "client.py"
        target.write_text("checkpoint\n", encoding="utf-8")
        manager = CheckpointManager(self.backend.settings)
        created = manager.create(
            self.session,
            self.index,
            ["client.py"],
            task_id="repository-provenance-race",
            repository_identity={"root": ".", "git_directory": ".git"},
        )
        target.write_text("agent\n", encoding="utf-8")
        manager.update_task_postimage(
            "repository-provenance-race",
            str(self.workspace),
            "client.py",
            True,
            hashlib.sha256(b"agent\n").hexdigest(),
        )

        async def change_repository_identity(_name: str, _description: str) -> bool:
            record = manager.load(str(created["checkpoint_id"]))
            record.repository_identity = {
                "root": "changed",
                "git_directory": ".git",
            }
            record.seal()
            path = self.history / "checkpoints" / f"{created['checkpoint_id']}.json"
            path.write_text(json.dumps(record.to_dict()), encoding="utf-8")
            return True

        self.approve.side_effect = change_repository_identity
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": created["checkpoint_id"], "paths": ["client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CHECKPOINT_INTEGRITY_FAILURE")
        self.assertEqual(target.read_text(), "agent\n")

    async def test_external_edit_blocks_restore_without_overwriting(self) -> None:
        (self.workspace / "client.py").write_text("baseline\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        (self.workspace / "client.py").write_text("agent\n", encoding="utf-8")
        manager = CheckpointManager(self.backend.settings)
        manager.update_task_postimage(
            "task-1", str(self.workspace), "client.py", True,
            hashlib.sha256(b"agent\n").hexdigest(),
        )
        (self.workspace / "client.py").write_text("external edit\n", encoding="utf-8")
        approvals_before = self.approve.await_count

        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CHECKPOINT_CONFLICT")
        self.assertEqual((self.workspace / "client.py").read_text(), "external edit\n")
        self.assertEqual(self.approve.await_count, approvals_before)

    async def test_unchanged_restore_is_checked_again_after_approval(self) -> None:
        target = self.workspace / "client.py"
        target.write_text("checkpoint A\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()

        async def modify_during_approval(_name: str, _description: str) -> bool:
            target.write_text("external B\n", encoding="utf-8")
            return True

        self.approve.side_effect = modify_during_approval
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CHECKPOINT_CONFLICT")
        self.assertEqual(target.read_text(encoding="utf-8"), "external B\n")

    async def test_changed_restore_is_revalidated_before_any_file_is_mutated(self) -> None:
        targets = {
            "a.py": "agent A\n",
            "b.py": "agent B\n",
        }
        for path, content in targets.items():
            (self.workspace / path).write_text(f"checkpoint {path}\n", encoding="utf-8")
        checkpoint = await self.tools.call(
            "git_checkpoint",
            {"task_id": "task-1", "paths": list(targets), "require_complete": True},
            session=self.session,
        )
        self.assertTrue(checkpoint["success"], checkpoint)
        manager = CheckpointManager(self.backend.settings)
        for path, content in targets.items():
            (self.workspace / path).write_text(content, encoding="utf-8")
            manager.update_task_postimage(
                "task-1", str(self.workspace), path, True,
                hashlib.sha256(content.encode()).hexdigest(),
            )

        async def modify_one_during_approval(_name: str, _description: str) -> bool:
            (self.workspace / "b.py").write_text("external B\n", encoding="utf-8")
            return True

        self.approve.side_effect = modify_one_during_approval
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint["checkpoint_id"], "paths": list(targets)},
            session=self.session,
        )

        self.assertEqual(result["error_code"], "CHECKPOINT_CONFLICT")
        self.assertEqual((self.workspace / "a.py").read_text(), "agent A\n")
        self.assertEqual((self.workspace / "b.py").read_text(), "external B\n")

    async def test_deleted_file_during_approval_is_reported_as_conflict(self) -> None:
        target = self.workspace / "client.py"
        target.write_text("checkpoint\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()

        async def delete_during_approval(_name: str, _description: str) -> bool:
            target.unlink()
            return True

        self.approve.side_effect = delete_during_approval
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertEqual(result["error_code"], "CHECKPOINT_CONFLICT")
        self.assertFalse(target.exists())

    async def test_created_missing_file_during_approval_is_preserved(self) -> None:
        checkpoint_id = await self.checkpoint("missing.py")
        target = self.workspace / "missing.py"

        async def create_during_approval(_name: str, _description: str) -> bool:
            target.write_text("external creation\n", encoding="utf-8")
            return True

        self.approve.side_effect = create_during_approval
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["missing.py"]},
            session=self.session,
        )

        self.assertEqual(result["error_code"], "CHECKPOINT_CONFLICT")
        self.assertEqual(target.read_text(), "external creation\n")

    async def test_symlink_substituted_during_approval_does_not_escape_workspace(self) -> None:
        target = self.workspace / "client.py"
        outside = Path(self.temp.name) / "outside.py"
        target.write_text("checkpoint\n", encoding="utf-8")
        outside.write_text("external\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()

        async def symlink_during_approval(_name: str, _description: str) -> bool:
            target.unlink()
            target.symlink_to(outside)
            return True

        self.approve.side_effect = symlink_during_approval
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertIn(result["error_code"], {
            "CHECKPOINT_CONFLICT", "CHECKPOINT_INTEGRITY_FAILURE",
        })
        self.assertEqual(outside.read_text(), "external\n")

    async def test_parent_path_substitution_during_approval_is_rejected(self) -> None:
        directory = self.workspace / "dir"
        directory.mkdir()
        target = directory / "client.py"
        target.write_text("checkpoint\n", encoding="utf-8")
        outside = Path(self.temp.name) / "outside-dir"
        outside.mkdir()
        (outside / "client.py").write_text("outside\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint("dir/client.py")

        async def replace_parent_during_approval(_name: str, _description: str) -> bool:
            directory.rename(self.workspace / "moved")
            directory.symlink_to(outside, target_is_directory=True)
            return True

        self.approve.side_effect = replace_parent_during_approval
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["dir/client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertIn(result["error_code"], {
            "CHECKPOINT_CONFLICT", "CHECKPOINT_INTEGRITY_FAILURE",
        })
        self.assertEqual((outside / "client.py").read_text(), "outside\n")

    async def test_restore_cancellation_during_worker_preflight_stops_before_approval(self) -> None:
        (self.workspace / "client.py").write_text("checkpoint\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        (self.workspace / "client.py").write_text("agent\n", encoding="utf-8")
        cancellation = threading.Event()
        original = RepositoryIndex.read_file_bytes
        reads = 0

        def cancel_after_first_read(index: RepositoryIndex, path: str, *, max_bytes: int) -> bytes | None:
            nonlocal reads
            reads += 1
            value = original(index, path, max_bytes=max_bytes)
            cancellation.set()
            return value

        approvals_before = self.approve.await_count
        with patch.object(RepositoryIndex, "read_file_bytes", cancel_after_first_read):
            result = await self.tools.call(
                "restore_checkpoint",
                {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
                session=self.session,
                cancellation=cancellation,
            )

        self.assertEqual(result["error_code"], "CANCELLED")
        self.assertEqual(reads, 1)
        self.assertEqual(self.approve.await_count, approvals_before)
        self.assertEqual((self.workspace / "client.py").read_text(), "agent\n")

    async def test_restore_preflight_runs_off_event_loop_and_timeout_cannot_authorize(self) -> None:
        (self.workspace / "client.py").write_text("checkpoint\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        (self.workspace / "client.py").write_text("agent\n", encoding="utf-8")
        original = RepositoryIndex.read_file_bytes
        entered = threading.Event()

        def slow_read(index: RepositoryIndex, path: str, *, max_bytes: int) -> bytes | None:
            entered.set()
            time.sleep(0.2)
            return original(index, path, max_bytes=max_bytes)

        ticked = asyncio.Event()

        async def heartbeat() -> None:
            await asyncio.sleep(0.02)
            ticked.set()

        self.backend.settings = replace(
            self.backend.settings,
            command_timeout=0.1,
        )
        approvals_before = self.approve.await_count
        with patch.object(RepositoryIndex, "read_file_bytes", slow_read):
            task = asyncio.create_task(self.tools.call(
                "restore_checkpoint",
                {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
                session=self.session,
            ))
            await heartbeat()
            result = await task
            self.assertTrue(entered.is_set())
            self.assertTrue(ticked.is_set())

        await asyncio.sleep(0.25)
        self.assertEqual(result["error_code"], "RESTORE_TIMEOUT")
        self.assertEqual(self.approve.await_count, approvals_before)
        self.assertEqual((self.workspace / "client.py").read_text(), "agent\n")

    async def test_restore_worker_exception_is_returned_as_structured_failure(self) -> None:
        (self.workspace / "client.py").write_text("checkpoint\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()

        def fail_read(_index: RepositoryIndex, _path: str, *, max_bytes: int) -> bytes | None:
            del max_bytes
            raise RuntimeError("backend read failed")

        with patch.object(RepositoryIndex, "read_file_bytes", fail_read):
            result = await self.tools.call(
                "restore_checkpoint",
                {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
                session=self.session,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CHECKPOINT_PREFLIGHT_FAILED")
        self.assertIn("backend read failed", result["error"])

    async def test_cancelled_preflight_worker_cannot_authorize_late_restore(self) -> None:
        (self.workspace / "client.py").write_text("checkpoint\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        target = self.workspace / "client.py"
        target.write_text("agent\n", encoding="utf-8")
        original = RepositoryIndex.read_file_bytes
        entered = threading.Event()
        cancellation = threading.Event()

        def slow_read(index: RepositoryIndex, path: str, *, max_bytes: int) -> bytes | None:
            entered.set()
            time.sleep(0.15)
            return original(index, path, max_bytes=max_bytes)

        approvals_before = self.approve.await_count
        with patch.object(RepositoryIndex, "read_file_bytes", slow_read):
            worker = asyncio.create_task(self.tools.call(
                "restore_checkpoint",
                {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
                session=self.session,
                cancellation=cancellation,
            ))
            while not entered.is_set():
                await asyncio.sleep(0.005)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        self.assertTrue(cancellation.is_set())
        await asyncio.sleep(0.2)
        self.assertEqual(self.approve.await_count, approvals_before)
        self.assertEqual(target.read_text(), "agent\n")

    async def test_cancellation_after_preflight_prevents_approval(self) -> None:
        (self.workspace / "client.py").write_text("checkpoint\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        approvals_before = self.approve.await_count
        guard = AsyncMock(side_effect=[True, False])

        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
            dispatch_guard=guard,
        )

        self.assertEqual(result["error_code"], "CANCELLED")
        self.assertEqual(self.approve.await_count, approvals_before)

    async def test_cancellation_while_approval_is_pending_prevents_mutation(self) -> None:
        target = self.workspace / "client.py"
        target.write_text("checkpoint\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        target.write_text("agent\n", encoding="utf-8")
        CheckpointManager(self.backend.settings).update_task_postimage(
            "task-1",
            str(self.workspace),
            "client.py",
            True,
            hashlib.sha256(b"agent\n").hexdigest(),
        )
        cancellation = threading.Event()

        async def cancel_during_approval(_name: str, _description: str) -> bool:
            cancellation.set()
            return True

        self.approve.side_effect = cancel_during_approval
        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
            cancellation=cancellation,
        )

        self.assertEqual(result["error_code"], "CANCELLED")
        self.assertEqual(target.read_text(), "agent\n")

    async def test_restore_preflights_every_file_before_mutating_any(self) -> None:
        (self.workspace / "a.py").write_text("a baseline\n", encoding="utf-8")
        (self.workspace / "b.py").write_text("b baseline\n", encoding="utf-8")
        checkpoint = await self.tools.call(
            "git_checkpoint",
            {"task_id": "task-1", "paths": ["a.py", "b.py"], "require_complete": True},
            session=self.session,
        )
        self.assertTrue(checkpoint["success"], checkpoint)
        (self.workspace / "a.py").write_text("a agent state\n", encoding="utf-8")
        (self.workspace / "b.py").write_text("b agent state\n", encoding="utf-8")
        manager = CheckpointManager(self.backend.settings)
        for path, content in (("a.py", b"a agent state\n"), ("b.py", b"b agent state\n")):
            manager.update_task_postimage(
                "task-1", str(self.workspace), path, True,
                hashlib.sha256(content).hexdigest(),
            )
        (self.workspace / "b.py").write_text("b external state\n", encoding="utf-8")
        approvals_before = self.approve.await_count

        result = await self.tools.call(
            "restore_checkpoint",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "paths": ["a.py", "b.py"],
            },
            session=self.session,
        )

        self.assertEqual(result["error_code"], "CHECKPOINT_CONFLICT")
        self.assertEqual((self.workspace / "a.py").read_text(), "a agent state\n")
        self.assertEqual((self.workspace / "b.py").read_text(), "b external state\n")
        self.assertEqual(self.approve.await_count, approvals_before)

    async def test_approval_denial_never_restores_a_checkpoint(self) -> None:
        (self.workspace / "client.py").write_text("baseline\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        denied = AsyncMock(return_value=False)
        tools = Tools(self.backend, denied)

        result = await tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "RESTORE_APPROVAL_DENIED")
        self.assertEqual((self.workspace / "client.py").read_text(), "baseline\n")
        self.assertEqual(denied.await_count, 1)

    async def test_checkpoint_capture_cancellation_creates_no_record(self) -> None:
        (self.workspace / "client.py").write_text("baseline\n", encoding="utf-8")
        cancellation = threading.Event()
        cancellation.set()

        result = await self.tools.call(
            "git_checkpoint",
            {"task_id": "", "paths": ["client.py"], "require_complete": True},
            session=self.session,
            cancellation=cancellation,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CANCELLED")
        self.assertFalse((self.history / "checkpoints").exists())

    async def test_checkpoint_cleanup_removes_expired_metadata_and_private_contents(self) -> None:
        (self.workspace / "client.py").write_text("baseline\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        manager = CheckpointManager(self.backend.settings)
        metadata = manager.directory / f"{checkpoint_id}.json"
        metadata.touch()
        os.utime(metadata, (1, 1))

        removed = manager.cleanup(older_than_seconds=1)

        self.assertEqual(removed, 1)
        self.assertFalse(metadata.exists())
        self.assertEqual(list((self.history / "checkpoint-snapshots").iterdir()), [])

    async def test_tampered_checkpoint_integrity_blocks_restoration(self) -> None:
        (self.workspace / "client.py").write_text("baseline\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        metadata = self.history / "checkpoints" / f"{checkpoint_id}.json"
        metadata.write_text("{}", encoding="utf-8")

        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CHECKPOINT_INTEGRITY_FAILURE")
        self.assertEqual(result["operation"], "restore_checkpoint")
        self.assertEqual((self.workspace / "client.py").read_text(), "baseline\n")

    async def test_symlink_replacement_blocks_restoration(self) -> None:
        target = self.workspace / "client.py"
        outside = Path(self.temp.name) / "outside.py"
        target.write_text("baseline\n", encoding="utf-8")
        outside.write_text("external\n", encoding="utf-8")
        checkpoint_id = await self.checkpoint()
        target.unlink()
        target.symlink_to(outside)

        result = await self.tools.call(
            "restore_checkpoint",
            {"checkpoint_id": checkpoint_id, "paths": ["client.py"]},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertIn(result["error_code"], {
            "CHECKPOINT_CONFLICT", "CHECKPOINT_INTEGRITY_FAILURE",
        })
        self.assertEqual(outside.read_text(), "external\n")

    async def test_checkpoint_storage_inside_workspace_is_rejected_without_creation(self) -> None:
        (self.workspace / "client.py").write_text("baseline\n", encoding="utf-8")
        settings = Settings(
            execution_mode="host",
            history_dir=self.workspace / ".synai-private",
        )
        backend = CheckpointBackend(self.workspace, self.workspace / ".synai-private")
        backend.settings = settings
        tools = Tools(backend, AsyncMock(return_value=True))

        result = await tools.call(
            "git_checkpoint",
            {"task_id": "", "paths": ["client.py"], "require_complete": True},
            session=self.session,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CHECKPOINT_SCOPE_VIOLATION")
        self.assertFalse((self.workspace / ".synai-private").exists())

    async def test_checkpoint_rejects_escape_and_reports_incomplete_capture(self) -> None:
        escape = await self.tools.call(
            "git_checkpoint",
            {"task_id": "", "paths": ["../outside"], "require_complete": True},
            session=self.session,
        )
        self.assertEqual(escape["error_code"], "INVALID_ARGUMENTS")

        missing = await self.tools.call(
            "git_checkpoint",
            {"task_id": "", "paths": ["missing.py"], "require_complete": True},
            session=self.session,
        )
        self.assertTrue(missing["success"], missing)
        self.assertTrue(missing["complete"])

        (self.workspace / "large.py").write_bytes(b"x" * (1_048_577))
        result = await self.tools.call(
            "git_checkpoint",
            {"task_id": "", "paths": ["large.py"], "require_complete": True},
            session=self.session,
        )
        self.assertEqual(result["error_code"], "CHECKPOINT_INCOMPLETE")
        self.assertFalse(result["success"])
