from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from synai.config import Settings
from synai.execution_backend import validate_workspace
from synai.models import Session
from synai.sandbox import SandboxError


async def stop_helper(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


class HostExecution:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspace: Path | None = None
        self.session_id: str | None = None

    @property
    def execution_workspace(self) -> Path | None:
        return self.workspace

    def activate(self, session: Session) -> None:
        self.revoke()
        if os.geteuid() == 0:
            raise SandboxError("Host tools cannot run as root")
        if sys.platform != "linux":
            raise SandboxError("Host tools currently require Linux")
        if session.environment is None or session.environment.execution_mode != "host":
            raise SandboxError("Conversation does not select host execution")
        self.workspace = validate_workspace(Path(session.workspace), self.settings)
        self.session_id = session.session_id

    def revoke(self) -> None:
        self.workspace, self.session_id = None, None

    def matches(self, session: Session) -> bool:
        return (
            session.environment is not None and session.environment.execution_mode == "host"
            and session.session_id == self.session_id and self.workspace == Path(session.workspace)
            and self.settings.execution_mode == "host" and os.geteuid() != 0
        )

    async def execute(
        self, name: str, arguments: dict[str, Any], expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        if self.session_id is None or self.workspace is None or self.settings.execution_mode != "host":
            raise SandboxError("Host tools are disconnected; enable them with approval in configuration")
        if os.geteuid() == 0:
            raise SandboxError("Host tools cannot run as root")
        workspace = validate_workspace(self.workspace, self.settings)
        payload = json.dumps({
            "name": name, "arguments": arguments, "limit": self.settings.output_bytes,
            "timeout": self.settings.command_timeout, "token": uuid4().hex,
            "expected_sha256": expected_sha256,
        }).encode()
        if len(payload) >= 4 * 1024 * 1024:
            raise SandboxError("Host tool request exceeds helper input limit")
        with tempfile.TemporaryDirectory(prefix="synai-host-") as temporary:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", "-u", str(Path(__file__).with_name("sandbox_helper.py")),
                "--host", str(workspace), temporary, cwd=workspace,
                env={"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
                     "HOME": temporary, "TMPDIR": temporary},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            ))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await spawn
                await asyncio.shield(stop_helper(process))
                raise
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None

            async def read(stream: asyncio.StreamReader, limit: int) -> bytes:
                chunks = bytearray()
                while data := await stream.read(65536):
                    chunks.extend(data)
                    if len(chunks) > limit:
                        raise SandboxError("Host helper exceeded transport output limit")
                return bytes(chunks)

            async def communicate() -> tuple[bytes, bytes]:
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
                stdout, stderr = await asyncio.gather(
                    read(process.stdout, self.settings.output_bytes * 8 + 65536),
                    read(process.stderr, 65536),
                )
                await process.wait()
                return stdout, stderr

            try:
                stdout, stderr = await asyncio.wait_for(communicate(), self.settings.command_timeout + 2)
            finally:
                await asyncio.shield(stop_helper(process))
            if process.returncode:
                raise SandboxError(stderr.decode(errors="replace").strip() or "Host helper failed")
            try:
                result = json.loads(stdout)
            except ValueError as exc:
                raise SandboxError("Invalid host tool response") from exc
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise SandboxError("Invalid host tool result")
            return result
