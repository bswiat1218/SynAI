from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from synai.config import Settings
from synai.execution_backend import validate_workspace
from synai.models import Session
from synai.storage import ConversationStorage


class SandboxError(Exception):
    pass


class Sandbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.container_id: str | None = None
        self.workspace: Path | None = None
        self.uid = max(1000, os.getuid())
        self.owner = uuid4().hex
        self.owned = False
        self.healthy = False

    @property
    def execution_workspace(self) -> Path | None:
        return Path("/workspace") if self.workspace is not None else None

    def matches(self, session: Session) -> bool:
        return (
            (session.environment is None or session.environment.execution_mode == "sandbox")
            and self.healthy and self.container_id == session.container_id
            and self.workspace == Path(session.workspace)
            and self.workspace == ConversationStorage(self.settings.history_dir).workspace(session.session_id)
        )

    async def _cli(self, *args: str, timeout: float = 30) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.runtime, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxError(f"Cannot start {self.settings.runtime}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise SandboxError(stderr.decode(errors="replace").strip() or "Container command failed")
        return stdout.decode("utf-8")

    async def pull(self, image: str) -> None:
        if not image or image.startswith("-") or any(char.isspace() for char in image):
            raise SandboxError("Invalid image reference")
        await self._cli("pull", image, timeout=600)

    async def create(self, workspace: Path, image: str) -> None:
        if self.container_id:
            raise SandboxError("Disconnect the current sandbox first")
        try:
            workspace = validate_workspace(workspace, self.settings, sandbox=True)
        except ValueError as exc:
            raise SandboxError(str(exc)) from exc
        if not workspace.is_dir() or "," in str(workspace):
            raise SandboxError("Select an existing workspace directory without commas in its path")
        if workspace in {Path("/"), Path.home().resolve()}:
            raise SandboxError("Do not expose root or your entire home directory")
        history = self.settings.history_dir.resolve()
        if history == workspace or workspace in history.parents:
            raise SandboxError("Workspace cannot expose the history directory")
        if not image or image.startswith("-") or any(char.isspace() for char in image):
            raise SandboxError("Invalid image reference")
        gid = max(1000, os.getgid())
        container = await self._cli(
            "run", "-d", "--label", f"local-coding-agent.owner={self.owner}",
            "--user", f"{self.uid}:{gid}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--network=bridge", "--memory", self.settings.memory,
            "--cpus", str(self.settings.cpus), "--pids-limit", str(self.settings.pids),
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            "--workdir", "/workspace", "--entrypoint", "python3",
            image, "-u", "-c", "import time; time.sleep(2147483647)",
        )
        self.container_id, self.workspace, self.owned = container.strip(), workspace, True
        try:
            await self.validate()
        except (SandboxError, TimeoutError):
            await self.remove_owned()
            raise

    async def attach(self, container: str, workspace: Path) -> None:
        if self.container_id:
            raise SandboxError("Disconnect the current sandbox first")
        if not container or container.startswith("-") or not all(char.isalnum() or char in "_.-" for char in container):
            raise SandboxError("Invalid container identifier")
        self.container_id, self.workspace = container, workspace.resolve(strict=True)
        self.owned = False
        try:
            await self.validate()
        except (SandboxError, TimeoutError):
            self.container_id, self.workspace, self.healthy = None, None, False
            raise

    async def validate(self) -> None:
        if not self.container_id or self.workspace is None:
            raise SandboxError("No sandbox selected")
        try:
            validate_workspace(self.workspace, self.settings, sandbox=True)
            inspect = json.loads(await self._cli("inspect", self.container_id))[0]
            self._validate_inspect(inspect)
            probe = json.loads(await self._cli(
                "exec", "--user", str(self.uid), self.container_id, "python3", "-c",
                "import json,os,sys,pathlib; p=pathlib.Path('/proc/self/status').read_text(); "
                "print(json.dumps({'uid':os.geteuid(),'version':list(sys.version_info[:2]),"
                "'nnp':'NoNewPrivs:\\t1' in p,'caps':'CapEff:\\t0000000000000000' in p,"
                "'writable':os.access('/workspace',os.W_OK)}))",
            ))
            if probe["uid"] == 0 or not probe["nnp"] or not probe["caps"] or probe["version"] < [3, 12]:
                raise SandboxError("Sandbox needs non-root Python 3.12+, no-new-privileges and zero effective capabilities")
            if not probe["writable"]:
                raise SandboxError("Sandbox user cannot write workspace; fix permissions outside this app")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SandboxError(f"Invalid container metadata: {exc}") from exc
        self.healthy = True

    def _validate_inspect(self, info: dict[str, Any]) -> None:
        if self.workspace is None:
            raise SandboxError("No sandbox workspace selected")
        try:
            validate_workspace(self.workspace, self.settings, sandbox=True)
        except (ValueError, OSError) as exc:
            raise SandboxError(str(exc)) from exc
        host, config = info["HostConfig"], info["Config"]
        if not info["State"]["Running"] or host.get("Privileged") or not host.get("ReadonlyRootfs"):
            raise SandboxError("Sandbox must be running, unprivileged and have a read-only root filesystem")
        if str(config.get("User", "")).split(":")[0] != str(self.uid):
            raise SandboxError(f"Container default user must be numeric UID {self.uid}, not root")
        if not any("no-new-privileges" in option for option in host.get("SecurityOpt") or []):
            raise SandboxError("Container must enable no-new-privileges")
        if not {"ALL", "all"}.intersection(host.get("CapDrop") or []) or host.get("CapAdd"):
            raise SandboxError("Container must drop ALL capabilities with no additions")
        for key in ("PidMode", "IpcMode", "NetworkMode"):
            if str(host.get(key, "")).startswith(("host", "container:")):
                raise SandboxError(f"Unsafe {key}")
        if host.get("Devices") or host.get("DeviceRequests") or host.get("GroupAdd"):
            raise SandboxError("Host devices and extra groups are forbidden")
        if not host.get("Memory") or not host.get("PidsLimit") or host["PidsLimit"] < 1:
            raise SandboxError("Container must have memory and PID limits")
        if not host.get("NanoCpus") and not host.get("CpuQuota"):
            raise SandboxError("Container must have a CPU limit")
        mounts = info.get("Mounts", [])
        if len(mounts) != 1:
            raise SandboxError("Only one workspace bind mount is permitted")
        mount = mounts[0]
        if mount.get("Type") != "bind" or mount.get("Destination") != "/workspace" or not mount.get("RW"):
            raise SandboxError("Sandbox must bind only the selected workspace read-write at /workspace")
        if Path(mount["Source"]).resolve() != self.workspace:
            raise SandboxError("Container workspace differs from selected directory")
        if self.workspace in {Path("/"), Path.home().resolve()}:
            raise SandboxError("Workspace is too broad")
        if self.settings.history_dir.resolve().is_relative_to(self.workspace):
            raise SandboxError("Workspace exposes conversation history")
        tmpfs = host.get("Tmpfs") or {}
        if set(tmpfs) - {"/tmp"}:
            raise SandboxError("Only /tmp may be a writable temporary filesystem")

    async def execute(self, name: str, arguments: dict[str, Any], expected_sha256: str | None = None) -> dict[str, Any]:
        await self.validate()
        assert self.container_id is not None
        token = uuid4().hex
        helper = Path(__file__).with_name("sandbox_helper.py").read_text(encoding="utf-8")
        payload = json.dumps({
            "name": name, "arguments": arguments, "limit": self.settings.output_bytes,
            "timeout": self.settings.command_timeout, "token": token, "expected_sha256": expected_sha256,
        }).encode()
        process = await asyncio.create_subprocess_exec(
            self.settings.runtime, "exec", "-i", "--user", str(self.uid),
            self.container_id, "python3", "-u", "-c", helper,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(payload), self.settings.command_timeout + 15)
        except (asyncio.CancelledError, TimeoutError):
            try:
                await asyncio.shield(self._cancel(token))
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise SandboxError(stderr.decode(errors="replace") or "Sandbox helper failed")
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise SandboxError("Invalid sandbox tool response") from exc
        if not isinstance(result, dict):
            raise SandboxError("Invalid sandbox tool result")
        return result

    async def _cancel(self, token: str) -> None:
        if self.container_id:
            await self._cli(
                "exec", "--user", str(self.uid), self.container_id, "python3", "-c",
                "import os,signal,pathlib\n"
                f"p=pathlib.Path('/tmp/coding-agent-{token}.pid')\n"
                f"pathlib.Path('/tmp/coding-agent-{token}.cancel').touch()\n"
                "try:\n"
                "    if p.exists(): os.kill(int(p.read_text()),signal.SIGTERM)\n"
                "except (FileNotFoundError,ProcessLookupError):\n"
                "    pass\n",
            )

    async def remove_owned(self) -> None:
        if self.owned and self.container_id:
            info = json.loads(await self._cli("inspect", self.container_id))[0]
            if info["Config"].get("Labels", {}).get("local-coding-agent.owner") != self.owner:
                raise SandboxError("Refusing to remove a container not owned by this application")
            await self._cli("rm", "-f", self.container_id)
        self.detach()

    def detach(self) -> None:
        self.container_id, self.workspace, self.healthy, self.owned = None, None, False, False
