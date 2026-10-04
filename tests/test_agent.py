from __future__ import annotations

from support import save_managed, bind_sandbox

import asyncio
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from textual import events
from textual._xterm_parser import XTermParser
from textual.widgets import Button, Input, RichLog, Select, SelectionList, Static, TextArea

from synai.agent import Agent
from synai.config import Settings
from synai.history import History, HistoryError
from synai.models import Activity, ChatEvent, Message, ModelInfo, Session
from synai.providers.base import ProviderError
from synai.providers.ollama import OllamaProvider
from synai.sandbox import Sandbox, SandboxError
from synai.sandbox_helper import ToolFailure, workspace_path
from synai.tools import Tools
from synai.tui.application import CodingApp


def call(name: str, arguments: dict[str, str]) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


class FakeProvider:
    def __init__(self) -> None:
        self.rounds = 0
        self.received = []

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo("test", True, True)]

    async def capabilities(self, name: str) -> ModelInfo:
        return ModelInfo(name, True, True)

    async def close(self) -> None:
        pass

    async def chat(self, model: str, messages: list[Message], tools: list[dict]):
        self.received.append(list(messages))
        self.rounds += 1
        if self.rounds == 1 and tools:
            yield ChatEvent(thinking="Checking the project.")
            yield ChatEvent(tool_calls=[call("terminal", {"command": "python -m unittest", "cwd": "."})], done=True)
        else:
            yield ChatEvent(content="Done.", done=True)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_preserves_reasoning_answer_and_native_calls(self) -> None:
        provider = OllamaProvider("http://localhost:11434")
        events = [
            {"message": {"thinking": "Reason"}},
            {"message": {"content": "Answer"}},
            {"message": {"tool_calls": [call("read_file", {"path": "main.py"})]}, "done": True},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/chat")
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            return httpx.Response(200, content="\n".join(json.dumps(event) for event in events))

        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = [event async for event in provider.chat("test", [Message("user", "Hello")], [])]
        self.assertEqual(result[0].thinking, "Reason")
        self.assertEqual(result[1].content, "Answer")
        self.assertTrue(result[2].done)
        self.assertEqual(result[2].tool_calls[0]["function"]["name"], "read_file")
        await provider.close()

    async def test_truncated_stream_errors_explicitly(self) -> None:
        provider = OllamaProvider("http://localhost:11434")
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content='{"message":{"content":"partial"}}\n')
        ))
        with self.assertRaises(ProviderError):
            _ = [event async for event in provider.chat("test", [], [])]
        await provider.close()


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_terminal_results_return_to_model_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings(), history_dir=Path(directory))
            sandbox = Sandbox(settings)
            sandbox.healthy = True
            sandbox.container_id = "test-container"
            sandbox.workspace = Path("/workspace")
            sandbox.execute = AsyncMock(return_value={"ok": False, "exit_code": 1, "stderr": "test failed"})
            approval = AsyncMock(return_value=True)
            provider = FakeProvider()
            history = History(Path(directory))
            agent = Agent(provider, history, Tools(sandbox, approval), AsyncMock())
            session = Session("test", "http://localhost:11434", "/workspace", container_id="test-container")
            bind_sandbox(sandbox, session)
            await agent.turn(session, ModelInfo("test", True, True), "Run tests")
            approval.assert_awaited_once()
            sandbox.execute.assert_awaited_once()
            self.assertEqual(session.messages[-1].content, "Done.")
            self.assertEqual(session.messages[2].thinking, "Checking the project.")
            self.assertEqual(provider.received[1][-1].role, "tool")
            self.assertIn("test failed", provider.received[1][-1].content)
            loaded = history.load(next(Path(directory).glob("*.json")))
            self.assertEqual(loaded.state, "idle")
            self.assertEqual(loaded.messages[-1].content, "Done.")

    async def test_denied_command_never_executes(self) -> None:
        sandbox = Sandbox(Settings())
        sandbox.execute = AsyncMock()
        tools = Tools(sandbox, AsyncMock(return_value=False))
        result = await tools.call("terminal", {"command": "echo test", "cwd": "."})
        self.assertTrue(result["denied"])
        sandbox.execute.assert_not_awaited()

    async def test_write_preview_and_diff_then_expected_hash(self) -> None:
        sandbox = Sandbox(Settings())
        sandbox.execute = AsyncMock(side_effect=[
            {"ok": True, "content": "old\n", "sha256": "oldhash"}, {"ok": True},
        ])
        approval = AsyncMock(return_value=True)
        tools = Tools(sandbox, approval)
        result = await tools.call("write_file", {"path": "main.txt", "content": "new\n"})
        self.assertTrue(result["ok"])
        self.assertIn("-old", approval.call_args.args[1])
        self.assertIn("+new", approval.call_args.args[1])
        self.assertEqual(sandbox.execute.call_args.args[2], "oldhash")

    async def test_unknown_tool_rejected_and_chat_only_has_no_tools(self) -> None:
        sandbox = Sandbox(Settings())
        sandbox.execute = AsyncMock()
        tools = Tools(sandbox, AsyncMock())
        result = await tools.call("sudo", {})
        self.assertFalse(result["ok"])
        sandbox.execute.assert_not_awaited()
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeProvider()
            agent = Agent(provider, History(Path(directory)), tools, AsyncMock())
            session = Session("test", "http://localhost", "/workspace")
            await agent.turn(session, ModelInfo("test"), "Hello")
            self.assertEqual(session.messages[-1].content, "Done.")
            self.assertEqual(provider.rounds, 1)

    async def test_recovery_does_not_replay_pending_tool(self) -> None:
        session = Session("test", "http://localhost", "/workspace", state="running")
        session.messages = [Message("user", "do things"), Message(
            "assistant", tool_calls=[call("terminal", {"command": "echo test", "cwd": "."})],
            status="streaming",
        )]
        Agent.recover(session)
        self.assertEqual(session.state, "interrupted")
        self.assertEqual(session.messages[-1].role, "tool")
        self.assertIn("not replayed", session.messages[-1].content)
        count = len(session.messages)
        Agent.recover(session)
        self.assertEqual(len(session.messages), count)

    async def test_budget_denial_resolves_calls_without_executing_them(self) -> None:
        class ManyCalls(FakeProvider):
            async def chat(self, model, messages, tools):
                yield ChatEvent(tool_calls=[
                    call("read_file", {"path": "one.txt"}),
                    call("read_file", {"path": "two.txt"}),
                ], done=True)

        with tempfile.TemporaryDirectory() as directory:
            sandbox = Sandbox(replace(Settings(), history_dir=Path(directory) / "data"))
            sandbox.healthy = True
            sandbox.container_id = "test-container"
            sandbox.workspace = Path("/workspace")
            sandbox.execute = AsyncMock(return_value={"ok": True, "content": "hello"})
            approval = AsyncMock(return_value=False)
            session = Session("test", "http://localhost", "/workspace", container_id="test-container")
            bind_sandbox(sandbox, session)
            agent = Agent(ManyCalls(), History(Path(directory)), Tools(sandbox, approval), AsyncMock(), budget=1)
            await agent.turn(session, ModelInfo("test", tools=True), "read")
            self.assertEqual(session.state, "stopped")
            self.assertEqual(sandbox.execute.await_count, 1)
            self.assertEqual(sum(message.role == "tool" for message in session.messages), 2)
            approval.assert_awaited_once()

    async def test_cancel_pending_action_resolves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Sandbox(replace(Settings(), history_dir=Path(directory) / "data"))
            sandbox.healthy = True
            sandbox.container_id = "test-container"
            sandbox.workspace = Path("/workspace")
            started = asyncio.Event()

            async def approve(name, detail):
                started.set()
                await asyncio.sleep(30)
                return True

            sandbox.execute = AsyncMock()
            session = Session("test", "http://localhost", "/workspace", container_id="test-container")
            bind_sandbox(sandbox, session)
            history = History(Path(directory))
            agent = Agent(FakeProvider(), history, Tools(sandbox, approve), AsyncMock())
            task = asyncio.create_task(agent.turn(session, ModelInfo("test", tools=True), "test"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            sandbox.execute.assert_not_awaited()
            loaded = history.load(next(Path(directory).glob("*.json")))
            self.assertEqual(loaded.state, "cancelled")
            self.assertEqual(loaded.messages[-1].role, "tool")
            self.assertIn("not replayed", loaded.messages[-1].content)

    async def test_corrupt_history_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{broken")
            with self.assertRaises(HistoryError):
                History(Path(directory)).load(path)

    async def test_history_delete_is_scoped_and_reports_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = History(root / "history")
            session = Session("test", "http://localhost", str(root / "workspace"))
            other = Session("test", "http://localhost", session.workspace)
            history.save(session)
            history.save(other)
            target = history.directory / f"{session.session_id}.json"
            outside = root / "outside.json"
            outside.write_text("{}")
            with self.assertRaises(HistoryError):
                history.delete(outside)
            history.delete(target)
            self.assertFalse(target.exists())
            self.assertTrue(outside.exists())
            self.assertEqual(len(history.list_paths()), 1)
            with self.assertRaises(HistoryError):
                history.delete(target)

    async def test_history_symlinks_do_not_expose_or_delete_outside_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = History(root / "history")
            history.directory.mkdir()
            outside = root / "keep.json"
            outside.write_text("{}")
            link = history.directory / "link.json"
            link.symlink_to(outside)
            self.assertEqual(history.list_paths(), [link])
            with self.assertRaises(HistoryError):
                history.load(link)
            with self.assertRaises(HistoryError):
                history.delete(link)
            self.assertTrue(outside.exists())
            self.assertTrue(link.is_symlink())


class SandboxTests(unittest.TestCase):
    def info(self, sandbox: Sandbox) -> dict:
        return {
            "State": {"Running": True},
            "Config": {"User": str(sandbox.uid)},
            "HostConfig": {
                "ReadonlyRootfs": True, "Privileged": False,
                "SecurityOpt": ["no-new-privileges"], "CapDrop": ["ALL"],
                "Memory": 1024 ** 3, "PidsLimit": 128, "NanoCpus": 2 * 10 ** 9,
                "Tmpfs": {"/tmp": "rw"},
            },
            "Mounts": [{"Type": "bind", "Source": str(sandbox.workspace), "Destination": "/workspace", "RW": True}],
        }

    def test_policy_rejects_privilege_socket_and_root(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        sandbox = Sandbox(replace(Settings(), history_dir=Path(temporary.name)))
        session = Session("test", "http://localhost", "")
        bind_sandbox(sandbox, session)
        sandbox._validate_inspect(self.info(sandbox))
        for section, key, value in [
            ("HostConfig", "Privileged", True),
            ("HostConfig", "NetworkMode", "host"),
            ("HostConfig", "CapAdd", ["SYS_ADMIN"]),
            ("Config", "User", "0"),
        ]:
            info = self.info(sandbox)
            info[section][key] = value
            with self.subTest(key=key), self.assertRaises(SandboxError):
                sandbox._validate_inspect(info)
        info = self.info(sandbox)
        info["Mounts"].append({"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"})
        with self.assertRaises(SandboxError):
            sandbox._validate_inspect(info)

    def test_workspace_paths_reject_traversal_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to("/tmp")
            with patch("synai.sandbox_helper.ROOT", root):
                self.assertEqual(workspace_path("main.py"), root / "main.py")
                for path in ("../bad", "/etc/passwd", "link/test", r"a\b"):
                    with self.subTest(path=path), self.assertRaises(ToolFailure):
                        workspace_path(path)


@unittest.skipUnless(os.getenv("AGENT_CONTAINER_TESTS") == "1", "Disposable container verification is opt-in")
class ContainerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonroot_file_tools_output_timeout_and_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = replace(
                Settings(), history_dir=root / "history", command_timeout=0.5, output_bytes=1024,
            )
            sandbox = Sandbox(settings)
            session = Session("test", "http://fake", "")
            bind_sandbox(sandbox, session)
            workspace = sandbox.workspace
            try:
                await sandbox.pull("python:3.12-slim")
                await sandbox.create(workspace, "python:3.12-slim")
                result = await sandbox.execute("write_file", {"path": "main.py", "content": "print(42)\n"})
                self.assertTrue(result["ok"], result)
                self.assertEqual((workspace / "main.py").read_text(), "print(42)\n")
                result = await sandbox.execute("terminal", {"command": "id -u; python3 main.py", "cwd": "."})
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["stdout"].splitlines(), [str(sandbox.uid), "42"])
                result = await sandbox.execute("terminal", {"command": "python3 -c 'import time; time.sleep(5)'", "cwd": "."})
                self.assertTrue(result["timed_out"], result)
                result = await sandbox.execute("terminal", {"command": "python3 -c 'print(\"x\" * 3000)'", "cwd": "."})
                self.assertTrue(result["truncated"], result)
                self.assertEqual(len(result["stdout"]), 1024)
                result = await sandbox.execute("read_file", {"path": "../outside"})
                self.assertFalse(result["ok"])
                result = await sandbox.execute("terminal", {"command": "sudo id", "cwd": "."})
                self.assertFalse(result["ok"])
                long_settings = replace(settings, command_timeout=10)
                sandbox.settings = long_settings
                task = asyncio.create_task(sandbox.execute(
                    "terminal", {"command": "python3 -c 'import time; time.sleep(20)'", "cwd": "."},
                ))
                await asyncio.sleep(0.8)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                result = await sandbox.execute("terminal", {"command": "echo responsive", "cwd": "."})
                self.assertTrue(result["ok"], result)
                token = UUID("01234567-89ab-cdef-0123-456789abcdef")
                await sandbox._cancel(token.hex)
                with patch("synai.sandbox.uuid4", return_value=token):
                    result = await sandbox.execute("write_file", {"path": "must-not-exist.txt", "content": "cancelled"})
                self.assertFalse(result["ok"], result)
                self.assertIn("Cancelled before execution", result["error"])
                self.assertFalse((workspace / "must-not-exist.txt").exists())

                class RepairProvider(FakeProvider):
                    async def chat(self, model, messages, tools):
                        self.received.append(list(messages))
                        self.rounds += 1
                        requests = {
                            1: [
                                call("write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"}),
                                call("write_file", {"path": "check.py", "content": "from calc import add\nassert add(2, 3) == 5\n"}),
                            ],
                            2: [call("terminal", {"command": "python3 check.py", "cwd": "."})],
                            3: [call("patch_file", {"path": "calc.py", "old": "return a - b", "new": "return a + b"})],
                            4: [call("terminal", {"command": "python3 check.py", "cwd": "."})],
                        }
                        if self.rounds in requests:
                            yield ChatEvent(tool_calls=requests[self.rounds], done=True)
                        else:
                            yield ChatEvent(content="Created two files, fixed the bug, and tests pass.", done=True)

                provider = RepairProvider()
                history = History(root / "history")
                approval = AsyncMock(return_value=True)
                agent = Agent(provider, history, Tools(sandbox, approval), AsyncMock())
                session.container_id = sandbox.container_id
                await agent.turn(session, ModelInfo("test", tools=True), "Create and test")
                self.assertEqual(session.state, "idle")
                self.assertIn("tests pass", session.messages[-1].content)
                self.assertIn("return a + b", (workspace / "calc.py").read_text())
                self.assertEqual(approval.await_count, 5)
                tool_results = [json.loads(message.content) for message in session.messages if message.role == "tool"]
                self.assertEqual([item["ok"] for item in tool_results], [True, True, False, True, True])
            finally:
                if sandbox.owned:
                    await sandbox.remove_owned()


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_synai_model_labels_and_legacy_activity_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                self.assertEqual(app.title, "SynAI")
                self.assertIn("SynAI", str(app.query_one("#neon-banner", Static).render()))
                saved = Session("qwen3:4b[local]", app.settings.ollama_url, directory)
                saved.messages = [
                    Message("user", "Run tests"),
                    Message("assistant", "Tests passed"),
                    Message("tool", '{"ok": true, "stdout": "All passed\\n"}', tool_name="terminal"),
                ]
                raw_result = 'terminal: {"ok": true, "stdout": "All passed\\n", "stderr": "", "exit_code": 0, "duration": 0.25}'
                saved.activity = [Activity("result", raw_result)]
                save_managed(app, saved)
                app.refresh_history()
                await app.open_saved_history(app.history.path_for(saved.session_id))
                await pilot.pause()
                chat = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
                activity = "\n".join(line.text for line in app.query_one("#activity", RichLog).lines)
                self.assertIn("qwen3:4b[local] [complete]", chat)
                self.assertNotIn("ASSISTANT", chat)
                self.assertIn("Run command // Result", " ".join(activity.split()))
                self.assertIn("Succeeded", activity)
                self.assertIn("STDOUT", activity)
                self.assertIn("All passed", activity)
                self.assertEqual(app.session.activity[0].text, raw_result)
                reloaded = app.history.load(app.history.path_for(saved.session_id))
                self.assertEqual(reloaded.messages[1].wire()["role"], "assistant")
                self.assertEqual(reloaded.messages[2].content, saved.messages[2].content)
                self.assertEqual(reloaded.activity[0].text, raw_result)

    async def test_compact_narrow_editor_remains_usable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                self.assertTrue(app.has_class("compact"))
                await app.create_session("test")
                self.assertTrue(app.has_class("narrow"))
                composer = app.query_one("#composer", TextArea)
                composer.focus()
                composer.scroll_visible(animate=False)
                await pilot.pause()
                self.assertGreaterEqual(composer.size.height, 3)
                self.assertGreaterEqual(app.query_one("#main").size.height, 14)
                await pilot.press("h", "i", "ctrl+enter")
                await pilot.pause()
                self.assertEqual(provider.rounds, 1)

    async def test_narrow_reasoning_panel_wraps_without_hidden_horizontal_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                text = "Inspect the workspace, create focused changes, and run the complete test suite."
                await app.create_session("test")
                app.session.messages.append(Message("assistant", "Ready", thinking=text))
                await app.render_session()
                app.flush_render()
                await pilot.pause()
                thinking = app.query_one("#thinking", RichLog)
                self.assertLessEqual(thinking.virtual_size.width, thinking.scrollable_content_region.width)
                self.assertEqual(" ".join(line.text.strip() for line in thinking.lines), text)

    async def test_terminal_sequences_decode_and_send_exactly_once(self) -> None:
        parser = XTermParser()
        ctrl_enter = list(parser.feed("\x1b[13;5u"))
        self.assertEqual([event.key for event in ctrl_enter if isinstance(event, events.Key)], ["ctrl+enter"])
        plain_enter = list(parser.feed("\r"))
        self.assertEqual([event.key for event in plain_enter if isinstance(event, events.Key)], ["enter"])
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer", TextArea)
                await app.create_session("test")
                composer.load_text("line one\nline two")
                composer.move_cursor((1, 8))
                composer.focus()
                for event in plain_enter:
                    app.post_message(event)
                await pilot.pause()
                self.assertEqual(composer.text, "line one\nline two\n")
                for event in ctrl_enter:
                    app.post_message(event)
                await pilot.pause()
                self.assertEqual(provider.rounds, 1)
                self.assertEqual(provider.received[-1][-1].content, "line one\nline two\n")

    async def test_history_manager_multiselect_confirmation_and_active_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await app.create_session("test")
                active = app.session.session_id
                other = Session("test", app.settings.ollama_url, directory, title="Delete this too")
                keep = Session("test", app.settings.ollama_url, directory, title="Keep")
                save_managed(app, other)
                save_managed(app, keep)
                sandbox_id = app.sandbox.container_id
                app.action_manage_history()
                await pilot.pause()
                choices = app.screen.query_one("#history-selection", SelectionList)
                self.assertTrue(app.screen.query_one("#delete-selected", Button).disabled)
                app.query_one("#composer", TextArea).load_text("Do not send behind modal")
                await pilot.press("ctrl+enter")
                self.assertEqual(provider.rounds, 0)
                choices.select(active)
                choices.select(other.session_id)
                await pilot.pause()
                await pilot.click("#delete-selected")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(set(choices.selected), {active, other.session_id})
                self.assertEqual(len(app.history.list_paths()), 3)
                await pilot.click("#delete-selected")
                await pilot.pause()
                await pilot.click("#allow")
                await pilot.pause()
                self.assertFalse((app.history.path_for(active)).exists())
                self.assertFalse((app.history.path_for(other.session_id)).exists())
                self.assertTrue((app.history.path_for(keep.session_id)).exists())
                self.assertIsNone(app.session)
                self.assertEqual(app.sandbox.container_id, sandbox_id)
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(app.history_manager_open)
                app.query_one("#composer", TextArea).load_text("New message")
                await pilot.press("ctrl+enter")
                await pilot.pause()
                self.assertFalse((app.history.path_for(active)).exists())

    async def test_bulk_delete_partial_failure_preserves_draft_and_failed_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await app.create_session("test")
                current = app.session
                app.query_one("#composer", TextArea).load_text("Keep draft")
                broken_id = "f" * 32
                app.storage.create(broken_id, workspace=False)
                broken = app.history.path_for(broken_id)
                broken.write_text("{bad")
                denied = Session("test", app.settings.ollama_url, directory)
                save_managed(app, denied)
                delete = app.history.delete

                def fail_one(path):
                    if app.history.identifier(path) == denied.session_id:
                        raise HistoryError("Permission denied for selected chat")
                    delete(path)

                app.action_manage_history()
                await pilot.pause()
                choices = app.screen.query_one("#history-selection", SelectionList)
                choices.select(broken_id)
                choices.select(denied.session_id)
                await pilot.pause()
                with patch.object(app.history, "delete", side_effect=fail_one):
                    await pilot.click("#delete-selected")
                    await pilot.pause()
                    await pilot.click("#allow")
                    await pilot.pause()
                self.assertFalse(broken.exists())
                self.assertTrue((app.history.path_for(denied.session_id)).exists())
                self.assertEqual(choices.selected, [denied.session_id])
                self.assertIn("Permission denied", str(app.screen.query_one("#history-result", Static).render()))
                self.assertIs(app.session, current)
                self.assertEqual(app.query_one("#composer", TextArea).text, "Keep draft")
                await pilot.press("escape")

    async def test_send_shortcuts_from_composer_preserve_plain_enter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer", TextArea)
                await app.create_session("test")
                for key in ("ctrl+enter", "ctrl+j", "ctrl+s"):
                    with self.subTest(key=key):
                        composer.load_text("First")
                        composer.move_cursor((0, 5))
                        composer.focus()
                        await pilot.press("enter")
                        self.assertEqual(composer.text, "First\n")
                        before = provider.rounds
                        await pilot.press(key)
                        await pilot.pause()
                        self.assertEqual(provider.rounds, before + 1)
                        self.assertEqual(provider.received[-1][-1].content, "First\n")
                        self.assertEqual(composer.text, "")

    async def test_delete_chat_confirmation_and_no_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_file = root / "keep.txt"
            workspace_file.write_text("unchanged")
            app = CodingApp(replace(Settings(), history_dir=root / "history"), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await app.create_session("test")
                original = app.session.session_id
                path = app.history.path_for(original)
                other = Session("test", app.settings.ollama_url, str(root))
                save_managed(app, other)
                app.refresh_history()
                task = asyncio.create_task(app.action_delete_history())
                await pilot.pause()
                await pilot.press("escape")
                await task
                self.assertTrue(path.exists())
                task = asyncio.create_task(app.action_delete_history())
                await pilot.pause()
                await pilot.press("ctrl+enter")
                self.assertEqual(provider.rounds, 0)
                await pilot.click("#allow")
                await task
                await pilot.pause()
                self.assertFalse(path.exists())
                self.assertIsNone(app.session)
                self.assertTrue((app.history.path_for(other.session_id)).exists())
                self.assertEqual(workspace_file.read_text(), "unchanged")
                app.query_one("#composer", TextArea).load_text("After deletion")
                await pilot.press("ctrl+enter")
                await pilot.pause()
                self.assertFalse(path.exists())

    async def test_corrupt_chat_can_be_deleted_and_failure_keeps_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = FakeProvider()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await app.create_session("test")
                current = app.session
                broken_id = "f" * 32
                app.storage.create(broken_id, workspace=False)
                path = app.history.path_for(broken_id)
                path.write_text("{broken")
                app.refresh_history()
                with patch.object(app.history, "delete", side_effect=HistoryError("Permission denied")):
                    task = asyncio.create_task(app.delete_histories([path]))
                    await pilot.pause()
                    await pilot.click("#allow")
                    await task
                self.assertTrue(path.exists())
                self.assertIs(app.session, current)
                task = asyncio.create_task(app.delete_histories([path]))
                await pilot.pause()
                await pilot.click("#allow")
                await task
                self.assertFalse(path.exists())
                self.assertIs(app.session, current)

    async def test_model_selection_composer_and_chat_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            fake = FakeProvider()
            app.provider = fake
            app.agent.provider = fake
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await pilot.pause()
                self.assertIsNone(app.session)
                self.assertEqual(len(app.query("#model")), 0)
                await app.create_session("test")
                self.assertEqual(app.session.model, "test")
                self.assertIsNotNone(app.session)
                app.query_one("#composer", TextArea).load_text("Hello")
                await pilot.click("#send")
                for _ in range(10):
                    await pilot.pause()
                    if app.turn_task is None:
                        break
                self.assertEqual(app.session.messages[-1].content, "Done.")
                self.assertEqual(app.session.state, "idle")
                self.assertGreater(len(app.query_one("#chat", RichLog).lines), 0)

    async def test_approval_dialog_deny_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            fake = FakeProvider()
            app.provider = fake
            app.agent.provider = fake
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                task = asyncio.create_task(app.approve("terminal", "echo hello"))
                await pilot.pause()
                await pilot.click("#deny")
                self.assertFalse(await task)
                self.assertIsNone(app.approval_future)

    async def test_narrow_layout_and_history_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            fake = FakeProvider()
            app.provider = fake
            app.agent.provider = fake
            async with app.run_test(size=(80, 45)) as pilot:
                await pilot.pause()
                self.assertTrue(app.has_class("narrow"))
                await app.create_session("test")
                app.query_one("#composer", TextArea).load_text("persist this")
                await app.action_send()
                await app.turn_task
                original_id = app.session.session_id
                await app.create_session("test")
                self.assertNotEqual(app.session.session_id, original_id)
                await app.open_saved_history(app.history.path_for(original_id))
                await pilot.pause()
                self.assertEqual(app.session.session_id, original_id)
                self.assertEqual(app.session.messages[-1].content, "Done.")

    async def test_shutdown_cancels_stream_and_closes_provider(self) -> None:
        class SlowProvider(FakeProvider):
            async def chat(self, model, messages, tools):
                yield ChatEvent(content="Partial answer")
                started.set()
                await asyncio.sleep(30)

        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()
            app = CodingApp(replace(Settings(), history_dir=Path(directory)), start_menu=False)
            await app.provider.close()
            provider = SlowProvider()
            provider.close = AsyncMock()
            app.provider = provider
            app.agent.provider = provider
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.create_session("test")
                app.query_one("#composer", TextArea).load_text("Start streaming")
                await app.action_send()
                await started.wait()
                task = app.turn_task
                app.query_one("#composer", TextArea).load_text("Do not send twice")
                await pilot.press("ctrl+enter")
                app.action_manage_history()
                await pilot.pause()
                self.assertIs(app.turn_task, task)
                self.assertFalse(app.history_manager_open)
                self.assertEqual(app.query_one("#composer", TextArea).text, "Do not send twice")
            self.assertTrue(task.done())
            provider.close.assert_awaited_once()
            self.assertEqual(app.session.state, "cancelled")
            self.assertEqual(app.session.messages[-1].content, "Partial answer")


if __name__ == "__main__":
    unittest.main()
