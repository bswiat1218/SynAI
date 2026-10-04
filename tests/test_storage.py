from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from synai.config import ConversationEnvironment, Settings
from synai.history import History, HistoryError, ManagedHistory
from synai.models import Session
from synai.sandbox import Sandbox, SandboxError
from synai.storage import ConversationStorage, SOURCE
from support import save_managed
import test_menu as menus
from textual.widgets import Input, Select, Static


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = ConversationStorage(self.root / ".synai")
        self.settings = replace(Settings(), history_dir=self.storage.root)
        self.history = ManagedHistory(self.storage, self.settings)

    def session(self, *, mode: str = "sandbox") -> Session:
        session = Session("m", self.settings.ollama_url, "", schema_version=4)
        workspace = self.storage.workspace(session.session_id) if mode == "sandbox" else self.root / "project"
        self.storage.create(session.session_id, workspace=mode == "sandbox")
        settings = replace(self.settings, execution_mode="host" if mode == "host" else "sandbox")
        session.set_environment(ConversationEnvironment.from_settings(settings, workspace))
        session.managed_workspace_created = mode == "sandbox"
        self.history.save(session)
        return session

    def test_exact_layout_private_permissions_roundtrip_and_independent_workspaces(self) -> None:
        first, second = self.session(), self.session()
        paths = self.history.list_paths()
        self.assertEqual(len(paths), 2)
        for session in (first, second):
            path = self.history.path_for(session.session_id)
            self.assertEqual(path, self.storage.root / "conversations" / session.session_id / "conversation.json")
            self.assertEqual(self.history.load(path).schema_version, 5)
            self.assertEqual(self.history.identifier(path), session.session_id)
            for private in (self.storage.root, self.storage.conversations, path.parent, path, Path(session.workspace)):
                self.assertEqual(private.stat().st_mode & 0o077, 0)
        (Path(first.workspace) / "unique.txt").write_text("first")
        self.assertFalse((Path(second.workspace) / "unique.txt").exists())
        self.assertFalse(any(path.name.endswith(".tmp") for path in paths))

    def test_source_and_forged_paths_symlinks_and_insecure_roots_refused(self) -> None:
        for path in (SOURCE, SOURCE / ".synai", SOURCE.parent):
            with self.assertRaisesRegex(ValueError, "overlap"):
                ConversationStorage(path)
        for identifier in ("..", "not-a-uuid", "a" * 31, "/" + "a" * 32):
            with self.assertRaises(ValueError):
                self.storage.folder(identifier)
        session = self.session()
        path = self.history.path_for(session.session_id)
        data = json.loads(path.read_text())
        data["workspace"] = data["environment"]["workspace"] = str(SOURCE)
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(HistoryError, "managed workspace"):
            self.history.load(path)
        workspace = self.storage.workspace(session.session_id)
        workspace.rmdir()
        workspace.symlink_to(SOURCE, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.storage.validate_workspace(workspace)
        symlink_root = self.root / "link"
        symlink_root.symlink_to(self.storage.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            ConversationStorage(symlink_root)
        insecure = self.root / "public"
        insecure.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            ConversationStorage(insecure).initialize()

    def test_delete_removes_managed_files_but_preserves_external_projects_and_symlink_targets(self) -> None:
        session = self.session(mode="host")
        project = Path(session.workspace)
        project.mkdir()
        (project / "keep.py").write_text("external")
        self.storage.create(session.session_id, workspace=True)
        workspace = self.storage.workspace(session.session_id)
        (workspace / "nested").mkdir()
        (workspace / "nested" / "generated.py").write_text("generated")
        (workspace / "external-link").symlink_to(project, target_is_directory=True)
        self.history.delete(self.history.path_for(session.session_id))
        self.assertFalse(self.storage.folder(session.session_id).exists())
        self.assertEqual((project / "keep.py").read_text(), "external")
        self.assertTrue(self.storage.root.exists())
        with self.assertRaises(HistoryError):
            self.history.delete(project / "keep.py")

    def test_partial_delete_retains_metadata_and_nested_mount_blocks_deletion(self) -> None:
        session = self.session()
        workspace = Path(session.workspace)
        child = workspace / "mount"
        child.mkdir()
        mount_check = Path.is_mount
        with patch.object(Path, "is_mount", lambda path: path == child or mount_check(path)):
            with self.assertRaisesRegex(HistoryError, "mount"):
                self.history.delete(self.history.path_for(session.session_id))
        self.assertTrue(self.history.path_for(session.session_id).exists())
        with patch("synai.storage.shutil.rmtree", side_effect=PermissionError("locked")):
            with self.assertRaisesRegex(HistoryError, "locked"):
                self.history.delete(self.history.path_for(session.session_id))
        self.assertTrue(self.history.load(self.history.path_for(session.session_id)))
        rmdir = Path.rmdir
        folder = self.storage.folder(session.session_id)

        def fail_folder(path: Path) -> None:
            if path == folder:
                raise PermissionError("cannot remove folder")
            rmdir(path)

        with patch.object(Path, "rmdir", fail_folder):
            with self.assertRaisesRegex(HistoryError, "cannot remove folder"):
                self.history.delete(self.history.path_for(session.session_id))
        self.assertTrue(self.history.load(self.history.path_for(session.session_id)))
        self.history.delete(self.history.path_for(session.session_id))

    def test_missing_workspace_does_not_recreate_files_on_load_or_save(self) -> None:
        session = self.session()
        Path(session.workspace).rmdir()
        self.history.load(self.history.path_for(session.session_id))
        self.history.save(session)
        self.assertFalse(Path(session.workspace).exists())


class StorageUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_draft_has_reserved_path_but_no_folder_cancel_and_source_is_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-new")
                screen = app.screen
                identifier = screen.conversation_id
                await menus.click(pilot, app, "#config-nav-workspace")
                field = screen.query_one("#env-workspace", Input)
                self.assertTrue(field.disabled)
                self.assertEqual(field.value, str(app.storage.workspace(identifier)))
                self.assertFalse(app.storage.folder(identifier).exists())
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(app.storage.folder(identifier).exists())
                await menus.MenuTests().create(pilot, app)
                workspace = Path(app.session.workspace)
                self.assertEqual(list(workspace.iterdir()), [])
                self.assertEqual(app.session.schema_version, 5)
                self.assertNotEqual(workspace, SOURCE)
                self.assertTrue(app.history.path_for(app.session.session_id).exists())

    async def test_host_path_draft_survives_toggling_without_mount_or_directory_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-new")
                screen = app.screen
                await menus.click(pilot, app, "#config-nav-sandbox")
                mode = screen.query_one("#env-execution_mode", Select)
                mode.value = "host"
                await pilot.pause()
                screen.query_one("#env-workspace", Input).value = str(project)
                mode.value = "sandbox"
                await pilot.pause()
                self.assertEqual(screen.query_one("#env-workspace", Input).value, screen.managed_workspace)
                mode.value = "host"
                await pilot.pause()
                self.assertEqual(screen.query_one("#env-workspace", Input).value, str(project))
                self.assertFalse(app.storage.folder(screen.conversation_id).exists())
                self.assertIsNone(app.sandbox.container_id)

    async def test_active_or_recorded_running_container_blocks_folder_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                session = app.session
                path = app.history.path_for(session.session_id)
                app.sandbox.container_id, app.sandbox.workspace = "running", Path(session.workspace)
                with self.assertRaisesRegex(HistoryError, "Disconnect"):
                    await app.check_workspace_unused(session.session_id, path)
                app.sandbox.detach()
                session.container_id = "running"
                app.history.save(session)
                with patch("synai.sandbox.Sandbox._cli", AsyncMock(side_effect=[
                    "running container-name\n", json.dumps([{"State": {"Running": True}}]),
                ])):
                    with self.assertRaisesRegex(HistoryError, "still running"):
                        await app.check_workspace_unused(session.session_id, path)
                with patch("synai.sandbox.Sandbox._cli", AsyncMock(side_effect=SandboxError("daemon offline"))):
                    with self.assertRaises(SandboxError):
                        await app.check_workspace_unused(session.session_id, path)
                with patch("synai.sandbox.Sandbox._cli", AsyncMock(return_value="")):
                    await app.check_workspace_unused(session.session_id, path)
                self.assertTrue(path.exists())

    async def test_confirmed_legacy_reset_keeps_projects_and_unverifiable_containers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {}, clear=True):
                old = home / ".local/share/local-coding-agent/history"
                project = home / "project"
                project.mkdir()
                sentinel = project / "keep.py"
                sentinel.write_text("old project")
                legacy = History(old)
                saved = Session("old", "http://localhost", str(project), container_id="old-container")
                legacy.save(saved)
                # Override the internal test root, not the public fixed-root CLI.
                from synai.tui.application import CodingApp
                app = CodingApp(replace(Settings(), history_dir=home / ".synai"), start_menu=False)
                async with app.run_test(size=(100, 36)) as pilot:
                    await pilot.pause()
                    approve = AsyncMock(side_effect=[True, True])
                    with patch.object(app, "approve", approve), patch(
                        "synai.sandbox.Sandbox._cli", AsyncMock(return_value=json.dumps([{
                            "Config": {"Labels": {"local-coding-agent.owner": "previous-app-instance"}},
                        }])),
                    ) as cli:
                        await app.offer_legacy_cleanup()
                    self.assertEqual(approve.await_count, 2)
                    self.assertEqual(cli.await_count, 1)
                    self.assertEqual(cli.call_args.args[0], "inspect")
                    self.assertFalse(legacy.path_for(saved.session_id).exists())
                    self.assertEqual(sentinel.read_text(), "old project")
                    self.assertIn("ownership unverifiable", app.legacy_report)
                    receipt = json.loads((app.storage.root / "legacy-cleanup.json").read_text())
                    self.assertEqual(receipt["status"], "complete")
                    with patch.object(app, "approve", AsyncMock()) as repeated:
                        await app.offer_legacy_cleanup()
                        repeated.assert_not_awaited()

    async def test_legacy_reset_denial_and_owned_removal_failure_preserve_histories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {}, clear=True):
                old = home / ".local/share/local-coding-agent/history"
                legacy = History(old)
                saved = Session("old", "http://localhost", str(home / "project"), container_id="owned")
                legacy.save(saved)
                from synai.tui.application import CodingApp
                app = CodingApp(Settings(), start_menu=False)
                async with app.run_test(size=(100, 36)) as pilot:
                    await pilot.pause()
                    with patch.object(app, "approve", AsyncMock(return_value=False)):
                        await app.offer_legacy_cleanup()
                    self.assertTrue(legacy.path_for(saved.session_id).exists())
                    self.assertEqual(json.loads((app.storage.root / "legacy-cleanup.json").read_text())["status"], "dismissed")
                    with patch.object(app, "approve", AsyncMock(return_value=True)), patch(
                        "synai.sandbox.Sandbox._cli", AsyncMock(return_value=json.dumps([{
                            "Config": {"Labels": {"local-coding-agent.owner": app.sandbox.owner}},
                        }])),
                    ), patch("synai.sandbox.Sandbox.remove_owned", AsyncMock(side_effect=SandboxError("cannot remove"))):
                        await app.offer_legacy_cleanup(retry=True)
                    self.assertTrue(legacy.path_for(saved.session_id).exists())
                    self.assertTrue(app.legacy_retry)
                    self.assertIn("cannot remove", app.legacy_report)


class ManagedSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_inspection_excludes_source_metadata_and_other_chat_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ConversationStorage(Path(directory) / ".synai")
            session = Session("m", "http://localhost", "")
            storage.create(session.session_id, workspace=True)
            sandbox = Sandbox(replace(Settings(), history_dir=storage.root))
            sandbox.workspace = storage.workspace(session.session_id)
            info = {
                "State": {"Running": True}, "Config": {"User": str(sandbox.uid)},
                "HostConfig": {
                    "ReadonlyRootfs": True, "Privileged": False, "SecurityOpt": ["no-new-privileges"],
                    "CapDrop": ["ALL"], "Memory": 1024 ** 3, "PidsLimit": 128, "NanoCpus": 2 * 10 ** 9,
                    "Tmpfs": {"/tmp": "rw"},
                },
                "Mounts": [{"Type": "bind", "Source": str(sandbox.workspace),
                            "Destination": "/workspace", "RW": True}],
            }
            sandbox._validate_inspect(info)
            other = Session("m", "http://localhost", "")
            storage.create(other.session_id, workspace=True)
            for source in (SOURCE, storage.root, storage.folder(session.session_id), storage.workspace(other.session_id)):
                changed = json.loads(json.dumps(info))
                changed["Mounts"][0]["Source"] = str(source)
                with self.assertRaises(SandboxError):
                    sandbox._validate_inspect(changed)
            extra = json.loads(json.dumps(info))
            extra["Mounts"].append({"Type": "bind", "Source": str(SOURCE), "Destination": "/app", "RW": False})
            with self.assertRaises(SandboxError):
                sandbox._validate_inspect(extra)

    async def test_exact_managed_mount_and_rejection_of_source_or_another_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ConversationStorage(Path(directory) / ".synai")
            session = Session("m", "http://localhost", "")
            storage.create(session.session_id, workspace=True)
            workspace = storage.workspace(session.session_id)
            sandbox = Sandbox(replace(Settings(), history_dir=storage.root))
            with patch.object(sandbox, "_cli", AsyncMock(return_value="container")) as cli, patch.object(
                sandbox, "validate", AsyncMock(),
            ):
                await sandbox.create(workspace, "python:3.12-slim")
            args = cli.call_args.args
            self.assertIn(f"type=bind,src={workspace},dst=/workspace", args)
            self.assertEqual(args.count("--mount"), 1)
            self.assertNotIn(str(SOURCE), args)
            sandbox.detach()
            for forbidden in (SOURCE, storage.root, storage.folder(session.session_id), Path(directory)):
                with self.assertRaises(SandboxError):
                    await sandbox.create(forbidden, "python:3.12-slim")
            other = Session("m", "http://localhost", "")
            storage.create(other.session_id, workspace=True)
            sandbox.workspace = workspace
            sandbox.container_id, sandbox.healthy = "container", True
            other.workspace, other.container_id = str(workspace), "container"
            self.assertFalse(sandbox.matches(other))
