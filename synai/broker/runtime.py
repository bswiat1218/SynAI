from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from synai.broker.config import BrokerConfig, BrokerReadiness
from synai.broker.protocol import BrokerError
from synai.broker.registry import BrokerRegistry


class RootlessPodmanRuntime:
    """Narrow Podman adapter; all runtime authority stays in the Broker process."""

    def __init__(self, config: BrokerConfig, registry: BrokerRegistry) -> None:
        self.config = config
        self.registry = registry
        self.readiness: BrokerReadiness | None = None
        self.image_id: str | None = None
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}

    async def verify_startup(self) -> BrokerReadiness:
        base = self.config.readiness()
        checks = dict(base.checks)
        reasons = list(base.reasons)
        if checks["podman_available"]:
            try:
                info = await self._run(
                    "info", "--format", "json", timeout=10, maximum=256 * 1024,
                )
                runtime = json.loads(info)
                host = runtime.get("host", runtime.get("Host", {}))
                security = host.get("security", host.get("Security", {}))
                rootless = security.get("rootless", security.get("Rootless", False))
                checks["rootless_podman"] = rootless is True
                checks["cgroup_v2"] = (
                    host.get("cgroupVersion", host.get("CgroupVersion")) == "v2"
                    and Path("/sys/fs/cgroup/cgroup.controllers").is_file()
                )
                checks["seccomp_profile"] = (
                    security.get("seccompEnabled", security.get("SeccompEnabled")) is True
                    and Path("/usr/share/containers/seccomp.json").is_file()
                )
                checks["linux_security_module"] = (
                    Path("/sys/kernel/security/lsm").exists()
                    and (
                        host.get("apparmorEnabled", host.get("AppArmorEnabled")) is True
                        or host.get("selinuxEnabled", host.get("SelinuxEnabled")) is True
                    )
                )
                uid_map = await self._run(
                    "unshare", "cat", "/proc/self/uid_map", timeout=10, maximum=4096,
                )
                ranges = []
                for line in uid_map.splitlines():
                    parts = line.split()
                    if len(parts) == 3:
                        ranges.append(tuple(int(value) for value in parts))
                checks["user_namespace"] = (
                    bool(ranges) and any(start == 0 and length == 1 for start, _, length in ranges)
                    and sum(length for _, _, length in ranges) >= 65536
                )
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError, BrokerError):
                checks["rootless_podman"] = False
                checks["cgroup_v2"] = False
                checks["seccomp_profile"] = False
                checks["user_namespace"] = False
                checks["linux_security_module"] = False
            if not checks["rootless_podman"] and "rootless_podman" not in reasons:
                reasons.append("rootless_podman")
            for name in ("cgroup_v2", "seccomp_profile", "user_namespace", "linux_security_module"):
                if not checks[name] and name not in reasons:
                    reasons.append(name)
            # An image must be supplied by an operator before startup. This
            # check never pulls and never accepts a tag or caller-selected name.
            try:
                inspected = await self._run(
                    "image", "inspect", self.config.image, "--format", "{{.Digest}} {{.Id}}",
                    timeout=10, maximum=4096,
                )
                values = inspected.split()
                checks["approved_image_present"] = bool(values and values[0] == self.config.image_digest)
                self.image_id = values[1] if len(values) == 2 else None
            except (OSError, TimeoutError, BrokerError):
                checks["approved_image_present"] = False
            if not checks["approved_image_present"] and "approved_image_present" not in reasons:
                reasons.append("approved_image_present")
        self.readiness = BrokerReadiness(
            not reasons, not reasons, checks, tuple(reasons),
        )
        return self.readiness

    async def create_task(self, task_id: str, allocation_id: str, workspace: Path) -> str:
        if self.readiness is None:
            raise BrokerError("broker_not_ready", "Broker startup verification has not completed.", 503)
        if not self.readiness.execution_available:
            raise BrokerError("execution_unavailable", "Required rootless container isolation is not available.", 503)
        container = await self._run(
            "create", "--name", f"synai-task-{allocation_id}",
            "--label", f"synai.broker.allocation={allocation_id}",
            "--label", f"synai.broker.task={task_id}",
            "--label", f"synai.broker.image-digest={self.config.image_digest.removeprefix('sha256:')}",
            "--user", "65532:65532",
            "--userns", "keep-id:uid=65532,gid=65532",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--security-opt", "seccomp=/usr/share/containers/seccomp.json",
            "--network", "none", "--ipc", "private",
            "--read-only", "--read-only-tmpfs=false",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--memory", str(self.config.profile.memory_bytes),
            "--cpus", str(self.config.profile.cpus),
            "--pids-limit", str(self.config.profile.pids),
            "--mount", f"type=bind,src={workspace},dst=/workspace,rw,rprivate,nodev,nosuid",
            "--workdir", "/workspace",
            self.config.image,
            "sleep", "infinity",
            timeout=15, maximum=4096,
        )
        container_id = container.strip()
        if len(container_id) < 12 or any(char not in "0123456789abcdef" for char in container_id):
            raise BrokerError("runtime_response_invalid", "Podman returned an invalid container identity.", 502)
        return container_id

    async def start_owned(self, task_id: str) -> None:
        owned = self.registry.owned_container(task_id)
        await self.inspect_owned(task_id)
        await self._run("start", owned["container_id"], timeout=15, maximum=4096)
        info = await self.inspect_owned(task_id)
        if info["State"]["Running"] is not True:
            self.registry.mark_container(task_id, "quarantined")
            raise BrokerError("container_start_unconfirmed", "Task container did not enter the running state.", 502)

    async def inspect_owned(self, task_id: str) -> dict[str, Any]:
        owned = self.registry.owned_container(task_id)
        return await self._inspect_record(task_id, owned)

    async def inspect_recorded(self, task_id: str) -> dict[str, Any]:
        owned = self.registry.container_record(task_id)
        if owned is None or not owned["container_id"]:
            raise BrokerError("container_not_owned", "No recorded container identity is available.", 404)
        return await self._inspect_record(task_id, owned)

    async def _inspect_record(self, task_id: str, owned: Any) -> dict[str, Any]:
        raw = await self._run("inspect", owned["container_id"], timeout=10, maximum=64 * 1024)
        try:
            details = json.loads(raw)
            if not isinstance(details, list) or len(details) != 1:
                raise ValueError("Container inspection shape is invalid")
            info = details[0]
            labels = info["Config"]["Labels"] or {}
            host = info["HostConfig"]
            mounts = info["Mounts"]
            workspace_mounts = [
                item for item in mounts
                if item.get("Type") == "bind" and item.get("Destination") == "/workspace"
            ]
            temp_mounts = [
                item for item in mounts
                if item.get("Type") == "tmpfs" and item.get("Destination") == "/tmp"
            ]
            expected_workspace = str(
                self.config.state_dir / "tasks" / task_id / "workspace",
            )
            expected_image_id = self.image_id
            security_options = host.get("SecurityOpt", [])
            if (
                info["Id"] != owned["container_id"]
                or labels.get("synai.broker.allocation") != owned["allocation_id"]
                or labels.get("synai.broker.task") != task_id
                or labels.get("synai.broker.image-digest") != owned["image_digest"]
                or expected_image_id is None or info["Image"] != expected_image_id
                or host.get("Privileged") is True
                or host.get("NetworkMode") != "none"
                or host.get("PidMode") in {"host", "container"}
                or host.get("IpcMode") in {"host", "container"}
                or host.get("ReadonlyRootfs") is not True
                or host.get("CapAdd")
                or "ALL" not in {str(value).upper() for value in host.get("CapDrop", [])}
                or not any("no-new-privileges" in option for option in security_options)
                or not any(
                    option == "seccomp=/usr/share/containers/seccomp.json"
                    for option in security_options
                )
                or host.get("Memory") != self.config.profile.memory_bytes
                or host.get("PidsLimit") != self.config.profile.pids
                or host.get("NanoCpus") != int(self.config.profile.cpus * 1_000_000_000)
                or not isinstance(mounts, list) or len(mounts) != 2
                or len(workspace_mounts) != 1
                or workspace_mounts[0].get("Source") != expected_workspace
                or workspace_mounts[0].get("RW") is not True
                or len(temp_mounts) != 1 or temp_mounts[0].get("RW") is not True
                or info["Config"].get("User") != "65532:65532"
                or not (
                    info.get("AppArmorProfile") not in {None, "", "unconfined"}
                    or bool(info.get("ProcessLabel"))
                )
            ):
                self.registry.mark_container(task_id, "quarantined")
                raise BrokerError("container_policy_mismatch", "Runtime container does not match its private ownership record or fixed policy.", 409)
            return info
        except (KeyError, TypeError, ValueError) as exc:
            self.registry.mark_container(task_id, "quarantined")
            raise BrokerError("container_inspection_invalid", "Runtime returned invalid task container evidence.", 502) from exc

    async def terminate_owned(self, task_id: str) -> str:
        return await self._terminate_container(task_id, recovery=False)

    async def terminate_recorded(self, task_id: str) -> str:
        return await self._terminate_container(task_id, recovery=True)

    async def _terminate_container(self, task_id: str, *, recovery: bool) -> str:
        owned = (
            self.registry.container_record(task_id)
            if recovery else self.registry.owned_container(task_id)
        )
        if owned is None or not owned["container_id"]:
            raise BrokerError("container_not_owned", "No recorded container identity is available.", 404)
        info = (
            await self.inspect_recorded(task_id)
            if recovery else await self.inspect_owned(task_id)
        )
        raw = json.dumps([info], sort_keys=True, separators=(",", ":"))
        if info["State"]["Running"] is True:
            await self._run("kill", owned["container_id"], timeout=10, maximum=4096)
            info = (
                await self.inspect_recorded(task_id)
                if recovery else await self.inspect_owned(task_id)
            )
            raw = json.dumps([info], sort_keys=True, separators=(",", ":"))
        try:
            items = json.loads(raw)
            if len(items) != 1 or items[0]["State"]["Running"] is not False:
                raise ValueError("Container is still running")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.registry.mark_container(task_id, "quarantined")
            raise BrokerError("termination_unconfirmed", "Task container termination could not be verified.", 409) from exc
        evidence = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if recovery:
            self.registry.record_reconciliation(task_id, "terminated", evidence)
        else:
            self.registry.mark_container(task_id, "terminated", termination=evidence)
        return evidence

    async def remove_owned(self, task_id: str) -> str:
        owned = self.registry.owned_container(task_id)
        if owned["lifecycle_state"] != "terminated":
            await self.terminate_owned(task_id)
            owned = self.registry.owned_container(task_id)
        await self.inspect_owned(task_id)
        await self._run("rm", owned["container_id"], timeout=10, maximum=4096)
        evidence = hashlib.sha256(
            f"{owned['container_id']}\n{owned['allocation_id']}\nremoved".encode(),
        ).hexdigest()
        self.registry.mark_container(task_id, "removed", cleanup=evidence)
        return evidence

    async def execute(
        self, task_id: str, argv: list[str], cwd: str, timeout: int, output_limit: int,
    ) -> dict[str, Any]:
        owned = self.registry.owned_container(task_id)
        await self.inspect_owned(task_id)
        workdir = "/workspace" if cwd == "." else f"/workspace/{cwd}"
        executable = shutil.which(self.config.podman)
        if executable is None:
            raise BrokerError("runtime_unavailable", "Rootless Podman is unavailable.", 503)
        process = await asyncio.create_subprocess_exec(
            executable, "exec", "--user", "65532:65532", "--workdir", workdir,
            owned["container_id"], *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin", "HOME": str(self.config.state_dir),
                "XDG_RUNTIME_DIR": f"/run/user/{self.config.service_uid}",
            },
        )
        self._active_processes[task_id] = process
        started = time.monotonic()
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(
                    _read_bounded(process.stdout, output_limit),
                    _read_bounded(process.stderr, output_limit),
                ),
                timeout=min(timeout, self.config.profile.operation_seconds),
            )
            await process.wait()
            if self.registry.task(task_id)["state"] != "running":
                raise BrokerError("operation_cancelled", "Task operation was cancelled.", 409)
        except (TimeoutError, asyncio.CancelledError, BrokerError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            try:
                await self.terminate_owned(task_id)
            except BrokerError:
                self.registry.mark_container(task_id, "quarantined")
            raise
        finally:
            if self._active_processes.get(task_id) is process:
                self._active_processes.pop(task_id, None)
        duration = time.monotonic() - started
        if len(stdout) + len(stderr) > output_limit:
            try:
                await self.terminate_owned(task_id)
            except BrokerError:
                self.registry.mark_container(task_id, "quarantined")
            raise BrokerError("operation_output_limit", "Task output exceeded its configured limit; the task container was terminated.", 413)
        return {
            "exit_status": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "duration_seconds": duration,
            "timed_out": False,
            "resource_limit_failure": False,
        }

    async def wait_cancelled_operation(self, task_id: str) -> None:
        process = self._active_processes.get(task_id)
        if process is None or process.returncode is not None:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _run(self, *args: str, timeout: float, maximum: int) -> str:
        if any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise BrokerError("runtime_argument_invalid", "Broker runtime argument is invalid.", 500)
        executable = shutil.which(self.config.podman)
        if executable is None:
            raise BrokerError("runtime_unavailable", "Rootless Podman is unavailable.", 503)
        process = await asyncio.create_subprocess_exec(
            executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.config.state_dir), "XDG_RUNTIME_DIR": f"/run/user/{self.config.service_uid}"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(
                    _read_bounded(process.stdout, maximum),
                    _read_bounded(process.stderr, 16 * 1024),
                ),
                timeout,
            )
            await process.wait()
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        except BrokerError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise BrokerError("runtime_operation_failed", "Rootless Podman task operation failed.", 502)
        return stdout.decode("utf-8", errors="strict")


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while len(result) <= limit:
        chunk = await stream.read(min(65536, limit + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
    raise BrokerError("operation_output_limit", "Task output exceeded its configured limit.", 413)
