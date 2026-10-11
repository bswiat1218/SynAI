from __future__ import annotations

from support import bind_sandbox

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

from synai.agent import Agent
from synai.config import Settings
from synai.history import History, HistoryError
from synai.models import Activity, ChatEvent, Message, ModelInfo, Session
from synai.providers.base import ModelUnavailableError, ProviderError
from synai.providers.ollama import OllamaProvider
from synai.sandbox import Sandbox, SandboxError
from synai.sandbox_helper import ToolFailure, workspace_path
from synai.tools import Tools


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
    async def test_capabilities_distinguish_completion_embedding_and_unknown(self) -> None:
        provider = OllamaProvider("http://localhost:11434")
        capabilities_by_model = {
            "chat": ["completion"],
            "chat-tools": ["completion", "tools"],
            "embedding": ["embedding"],
            "thinking-only": ["thinking"],
            "missing": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/show")
            model = json.loads(request.content)["model"]
            capabilities = capabilities_by_model[model]
            body = {} if capabilities is None else {"capabilities": capabilities}
            return httpx.Response(200, json=body)

        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            self.assertIs((await provider.capabilities("chat")).chat, True)
            self.assertIs((await provider.capabilities("chat-tools")).chat, True)
            self.assertTrue((await provider.capabilities("chat-tools")).tools)
            self.assertIs((await provider.capabilities("embedding")).chat, False)
            self.assertIsNone((await provider.capabilities("thinking-only")).chat)
            self.assertTrue((await provider.capabilities("thinking-only")).thinking)
            self.assertIsNone((await provider.capabilities("missing")).chat)
        finally:
            await provider.close()

    async def test_malformed_capability_metadata_is_reported_as_unknown(self) -> None:
        provider = OllamaProvider("http://localhost:11434")
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"capabilities": ["completion", 3]})
        ))
        try:
            capability = await provider.capabilities("malformed")
            self.assertIsNone(capability.chat)
            self.assertIsNotNone(capability.capability_error)
        finally:
            await provider.close()

    async def test_missing_model_from_show_is_candidate_scoped(self) -> None:
        provider = OllamaProvider("http://localhost:11434")
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(404, json={"error": "model not found"})
        ))
        try:
            with self.assertRaises(ModelUnavailableError):
                await provider.capabilities("removed")
        finally:
            await provider.close()

    async def test_show_transport_failure_remains_provider_error(self) -> None:
        provider = OllamaProvider("http://localhost:11434")
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": "unavailable"})
        ))
        try:
            with self.assertRaises(ProviderError) as raised:
                await provider.capabilities("model")
            self.assertNotIsInstance(raised.exception, ModelUnavailableError)
        finally:
            await provider.close()

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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            sandbox = Sandbox(replace(
                Settings(), history_dir=root / "history",
            ))
            sandbox.workspace = workspace
            sandbox.execute = AsyncMock(side_effect=[
                {"ok": True, "content": "old\n", "sha256": "oldhash"}, {"ok": True},
            ])
            approval = AsyncMock(return_value=True)
            tools = Tools(sandbox, approval)
            result = await tools.call(
                "write_file", {"path": "main.txt", "content": "new\n"},
            )
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

    async def test_chat_only_rejects_malicious_native_tool_calls_without_dispatch(self) -> None:
        class MaliciousProvider:
            def __init__(self) -> None:
                self.received_tools: list[list[dict]] = []

            async def chat(self, model, messages, tools):
                self.received_tools.append(tools)
                yield ChatEvent(
                    content="I will inspect files.",
                    tool_calls=[call("terminal", {"command": "touch /tmp/should-not-run"})],
                    done=True,
                )

        class InMemoryHistory:
            def save(self, _session) -> None:
                pass

        provider = MaliciousProvider()
        session = Session("test", "http://localhost:11434", "")
        agent = Agent(
            provider, InMemoryHistory(), None, AsyncMock(),
            tool_execution_enabled=False,
        )
        await agent.turn(session, ModelInfo("test", tools=True), "Read my files")
        self.assertEqual(session.state, "error")
        self.assertEqual(provider.received_tools, [[]])
        self.assertEqual(session.messages[-1].content, "I will inspect files.")
        self.assertEqual(session.messages[-1].tool_calls, [])
        self.assertNotIn("workspace", session.messages[0].content.lower())

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


if __name__ == "__main__":
    unittest.main()
