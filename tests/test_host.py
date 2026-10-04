from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import AsyncMock, patch

from synai.agent import Agent
from synai.config import ConversationEnvironment, Settings
from synai.history import History
from synai.host import HostExecution
from synai.models import ChatEvent, ModelInfo, Session
from synai.sandbox import SandboxError
from synai.tools import Tools, schemas


class HostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.settings = replace(
            Settings(), history_dir=self.root / "history", execution_mode="host",
            command_timeout=1, output_bytes=4096,
        )
        self.session = Session("model", self.settings.ollama_url, str(self.workspace))
        self.session.set_environment(ConversationEnvironment.from_settings(self.settings, self.workspace))
        self.host = HostExecution(self.settings)
        self.host.activate(self.session)
        self.approve = AsyncMock(return_value=True)
        self.tools = Tools(self.host, self.approve)

    async def test_real_file_tools_multifile_edits_and_approval_denial(self) -> None:
        for path, content in (("main.py", "print('hi')\n"), ("web/index.html", "<p>Hello</p>")):
            result = await self.tools.call("write_file", {"path": path, "content": content})
            self.assertTrue(result["ok"], result)
            self.assertEqual((self.workspace / path).read_text(), content)
        self.assertIn("HOST EXECUTION", self.approve.call_args.args[1])
        self.assertTrue((await self.tools.call("patch_file", {
            "path": "main.py", "old": "'hi'", "new": "'hello'",
        }))["ok"])
        self.assertEqual((await self.tools.call("read_file", {"path": "main.py"}))["content"], "print('hello')\n")
        self.assertTrue((await self.tools.call("list_files", {"path": "."}))["ok"])
        self.approve.return_value = False
        denied = await self.tools.call("delete_file", {"path": "main.py"})
        self.assertTrue(denied["denied"])
        self.assertTrue((self.workspace / "main.py").exists())
        self.approve.return_value = True
        self.assertTrue((await self.tools.call("delete_file", {"path": "main.py"}))["ok"])

    async def test_paths_and_changed_since_approval_rejected(self) -> None:
        outside = self.root / "outside"
        outside.write_text("secret")
        (self.workspace / "link").symlink_to(outside)
        for path in ("../outside", str(outside), "link"):
            result = await self.tools.call("read_file", {"path": path})
            self.assertFalse(result["ok"], result)
        target = self.workspace / "target"
        target.write_text("original")

        async def racing_approval(*args):
            target.write_text("changed by another process")
            return True

        self.tools.approve = racing_approval
        result = await self.tools.call("write_file", {"path": "target", "content": "model content"})
        self.assertFalse(result["ok"])
        self.assertIn("changed since approval", result["error"])
        self.assertEqual(target.read_text(), "changed by another process")

    async def test_command_results_environment_and_no_new_privileges(self) -> None:
        command = """python3 -c "import os,pathlib,sys; print('hello'); print(os.getcwd()); print(os.getenv('SYNAI_TEST_SECRET')); print(os.getenv('HOME')); print(os.getenv('TMPDIR')); print(next(x for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith('NoNewPrivs:'))); print('bad',file=sys.stderr); sys.exit(3)" """
        with patch.dict(os.environ, {"SYNAI_TEST_SECRET": "do-not-inherit"}):
            result = await self.tools.call("terminal", {"command": command, "cwd": "."})
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["stderr"], "bad\n")
        self.assertGreater(result["duration"], 0)
        lines = result["stdout"].splitlines()
        self.assertEqual(lines[1], str(self.workspace))
        self.assertEqual(lines[2], "None")
        self.assertEqual(lines[3], lines[4])
        self.assertFalse(Path(lines[3]).exists())
        self.assertEqual(lines[5], "NoNewPrivs:\t1")

    async def test_timeout_and_exact_output_bound(self) -> None:
        started = time.monotonic()
        result = await self.tools.call("terminal", {"command": "printf partial; sleep 10", "cwd": "."})
        self.assertTrue(result["timed_out"], result)
        self.assertIn("partial", result["stdout"])
        self.assertLess(time.monotonic() - started, 5)
        result = await self.tools.call("terminal", {
            "command": "python3 -c \"import sys; sys.stdout.write('a'*100000)\"", "cwd": ".",
        })
        self.assertTrue(result["truncated"], result)
        self.assertEqual(len(result["stdout"].encode()) + len(result["stderr"].encode()), 4096)

    async def test_cancellation_stops_ordinary_child_and_cleans_temporary_home(self) -> None:
        self.host.settings = replace(self.settings, command_timeout=30)
        task = asyncio.create_task(self.tools.call("terminal", {
            "command": "printf '%s' \"$HOME\" > home; sleep 20 & printf '%s' \"$!\" > child; wait",
            "cwd": ".",
        }))
        for _ in range(100):
            if (self.workspace / "child").exists() and (self.workspace / "child").read_text():
                break
            await asyncio.sleep(0.02)
        self.assertTrue((self.workspace / "child").exists())
        pid = int((self.workspace / "child").read_text())
        home = Path((self.workspace / "home").read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        status = Path(f"/proc/{pid}/status")
        self.assertTrue(not status.exists() or "State:\tZ" in status.read_text())
        self.assertFalse(home.exists())

    async def test_cancel_during_spawn_stops_helper_without_running_action(self) -> None:
        real_spawn = asyncio.create_subprocess_exec
        spawning = asyncio.Event()
        release = asyncio.Event()
        processes = []

        async def delayed_spawn(*args, **kwargs):
            spawning.set()
            await release.wait()
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            return process

        with patch("synai.host.asyncio.create_subprocess_exec", delayed_spawn):
            task = asyncio.create_task(self.tools.call("terminal", {
                "command": "touch must-not-run", "cwd": ".",
            }))
            await spawning.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse((self.workspace / "must-not-run").exists())

    async def test_root_no_consent_and_privilege_requests_refused(self) -> None:
        with patch("synai.host.os.geteuid", return_value=0):
            with self.assertRaisesRegex(SandboxError, "root"):
                self.host.activate(self.session)
        self.assertFalse(self.host.matches(self.session))
        self.host.activate(self.session)
        for command in ("sudo id", "su root", "doas id", "eval 'id'"):
            result = await self.tools.call("terminal", {"command": command, "cwd": "."})
            self.assertFalse(result["ok"])
            self.assertIn("privilege escalation", result["error"])
        self.host.revoke()
        result = await self.tools.call("terminal", {"command": "touch forbidden", "cwd": "."})
        self.assertFalse(result["ok"])
        self.assertFalse((self.workspace / "forbidden").exists())
        self.assertFalse(self.host.matches(self.session))

    async def test_workspace_and_session_readiness_binding(self) -> None:
        self.assertTrue(self.host.matches(self.session))
        self.assertFalse(self.host.matches(replace(self.session, session_id="different")))
        self.assertFalse(self.host.matches(replace(self.session, workspace=str(self.root))))
        for workspace in (Path("/"), Path.home(), self.root, self.settings.history_dir):
            if workspace == self.settings.history_dir:
                workspace.mkdir()
            invalid = replace(self.session, workspace=str(workspace))
            with self.assertRaises((ValueError, SandboxError)):
                self.host.activate(invalid)
            self.assertIsNone(self.host.session_id)

    async def test_http_bounded_and_invalid_scheme_refused(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 8192)

            def log_message(self, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            result = await self.tools.call("fetch_url", {"url": f"http://127.0.0.1:{server.server_port}/"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], 200)
            self.assertEqual(len(result["text"]), 4096)
            self.assertTrue(result["truncated"])
            self.assertFalse((await self.tools.call("fetch_url", {"url": "file:///etc/passwd"}))["ok"])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_no_new_privileges_failure_is_explicit(self) -> None:
        # Run the same trusted helper setup with a simulated kernel refusal.
        import subprocess
        import sys

        from synai import sandbox_helper
        helper = Path(sandbox_helper.__file__)
        bootstrap = (
            "import ctypes,runpy,sys; "
            "ctypes.CDLL=lambda *a,**k:type('Kernel',(),{'prctl':lambda *a:-1})(); "
            f"sys.argv={[str(helper), '--host', str(self.workspace), str(self.root)]!r}; "
            f"runpy.run_path({str(helper)!r},run_name='__main__')"
        )
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-I", "-c", bootstrap],
                                         capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Cannot enable no-new-privileges", result.stderr)

    async def test_agent_eligibility_context_and_real_tool_result(self) -> None:
        received = []
        class Provider:
            async def chat(self, model, messages, tools):
                received.append((messages, tools))
                if len(received) == 1:
                    yield ChatEvent(tool_calls=[{"function": {
                        "name": "terminal", "arguments": {"command": "printf verified", "cwd": "."},
                    }}], done=True)
                else:
                    yield ChatEvent(content="Done", done=True)

        history = History(self.settings.history_dir)
        agent = Agent(Provider(), history, self.tools, AsyncMock())
        await agent.turn(self.session, ModelInfo("model", tools=True), "Run a test")
        self.assertEqual(self.session.state, "idle")
        self.assertTrue(received[0][1])
        self.assertIn("NOT isolated", received[0][0][1].content)
        self.assertEqual(json.loads(self.session.messages[-2].content)["stdout"], "verified")
        self.assertEqual(sum(m.role == "system" for m in self.session.messages), 1)
        self.host.revoke()
        received.clear()
        await agent.turn(self.session, ModelInfo("model", tools=True), "Try again")
        self.assertEqual(received[0][1], [])
        self.assertEqual(self.session.state, "error")
        self.assertIn("tools are disabled", self.session.activity[-1].text)
        self.assertIn("directly on the host", next(
            schema["function"]["description"] for schema in schemas("host")
            if schema["function"]["name"] == "terminal"
        ))
