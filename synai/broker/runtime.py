from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from synai.broker.config import BrokerConfig, BrokerReadiness
from synai.broker.protocol import BrokerError
from synai.broker.registry import BrokerRegistry
from synai.broker.filesystem import Directory


ContainmentState = Literal[
    "verified_stopped", "verified_absent", "still_running",
    "runtime_unavailable", "identity_mismatch", "inspection_inconclusive",
]


@dataclass(frozen=True)
class ContainmentResult:
    state: ContainmentState
    evidence: str | None = None
    error_code: str | None = None

    @property
    def verified(self) -> bool:
        return self.state in {"verified_stopped", "verified_absent"}


class RootlessPodmanRuntime:
    """Narrow Podman adapter; all runtime authority stays in the Broker process."""

    def __init__(self, config: BrokerConfig, registry: BrokerRegistry) -> None:
        self.config = config
        self.registry = registry
        self.readiness: BrokerReadiness | None = None
        self.image_id: str | None = None
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self.workspace_resolver: Callable[[str], Path] | None = None
        self.workspace_authority: Callable[[str], Directory] | None = None
        self.storage_containment_check: Callable[[], bool] | None = None
        self._containment_locks: dict[str, asyncio.Lock] = {}

    async def verify_startup(self, base_readiness: BrokerReadiness | None = None) -> BrokerReadiness:
        base = base_readiness or self.config.readiness()
        checks = dict(base.checks)
        runtime_checks = {
            "rootless_podman", "cgroup_v2", "seccomp_profile",
            "linux_security_module", "user_namespace",
            "approved_image_present", "storage_layout",
        }
        reasons = [reason for reason in base.reasons if reason not in runtime_checks]
        if checks["podman_available"]:
            try:
                info = await self._run(
                    "info", "--format", "json", timeout=10, maximum=256 * 1024,
                )
                runtime = json.loads(info)
                if not isinstance(runtime, dict):
                    raise ValueError("Podman information response is not an object")
                host = runtime.get("host", runtime.get("Host", {}))
                if not isinstance(host, dict):
                    raise ValueError("Podman host information is invalid")
                security = host.get("security", host.get("Security", {}))
                if not isinstance(security, dict):
                    raise ValueError("Podman security information is invalid")
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
                store = runtime.get("store", runtime.get("Store", {}))
                if not isinstance(store, dict):
                    raise ValueError("Podman storage information is invalid")
                checks["storage_layout"] = self._verify_reported_storage_layout(store)
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
                checks["storage_layout"] = False
            if not checks["rootless_podman"] and "rootless_podman" not in reasons:
                reasons.append("rootless_podman")
            for name in (
                "cgroup_v2", "seccomp_profile", "user_namespace",
                "linux_security_module", "storage_layout",
            ):
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
                checks["approved_image_present"] = (
                    len(values) == 2
                    and values[0] == self.config.image_digest
                    and bool(values[1])
                )
                self.image_id = values[1] if len(values) == 2 else None
            except (OSError, TimeoutError, BrokerError):
                checks["approved_image_present"] = False
            if not checks["approved_image_present"] and "approved_image_present" not in reasons:
                reasons.append("approved_image_present")
        self.readiness = BrokerReadiness(
            not reasons, not reasons, checks, tuple(reasons),
        )
        return self.readiness

    def _verify_reported_storage_layout(self, store: dict[str, Any]) -> bool:
        roles = {role.kind: role for role in self.config.storage_roles}
        expected_fields = {
            "podman_graph": ("graphRoot", "GraphRoot"),
            "podman_runtime": ("runRoot", "RunRoot"),
            "image_scratch": ("imageCopyTmpDir", "ImageCopyTmpDir"),
        }
        for kind, names in expected_fields.items():
            role = roles.get(kind)
            actual = next((store.get(name) for name in names if name in store), None)
            if role is None or not isinstance(actual, str):
                return False
            if Path(actual) != role.path:
                return False
        state = roles.get("broker_state")
        if state is None or state.path != self.config.state_dir:
            return False
        temporary = roles.get("temporary")
        if not roles.get("broker_logs") or temporary is None:
            return False
        try:
            for kind in (
                "podman_graph", "podman_runtime", "image_scratch",
                "broker_logs", "temporary", "broker_state",
            ):
                role = roles[kind]
                if not role.path.is_dir() or role.path.is_symlink():
                    return False
        except OSError:
            return False
        return True

    async def create_task(self, task_id: str, allocation_id: str, workspace: Path) -> str:
        self.registry.require_execution_authority()
        if self.readiness is None:
            raise BrokerError("broker_not_ready", "Broker startup verification has not completed.", 503)
        if not self.readiness.execution_available:
            raise BrokerError("execution_unavailable", "Required rootless container isolation is not available.", 503)
        if self.workspace_authority is None:
            raise BrokerError("bind_source_unverified", "Pinned workspace authority is unavailable.", 503)
        with self.workspace_authority(task_id) as source:
            record = self.registry.container_record(task_id)
            if source.path != workspace or record is None or record["allocation_id"] != allocation_id:
                raise BrokerError("bind_source_unverified", "Workspace allocation identity is inconsistent.", 503)
            source.revalidate()
        # Podman CLI resolves the source later; no accepted descriptor handoff exists.
        raise BrokerError(
            "bind_source_resolution_unaccepted",
            "External runtime bind-source resolution has not passed identity-preserving acceptance.",
            503,
        )

    def create_arguments(self, task_id: str, allocation_id: str, workspace: Path) -> tuple[str, ...]:
        return (
            "create", "--name", f"synai-task-{allocation_id}",
            "--label", f"synai.broker.allocation={allocation_id}",
            "--label", f"synai.broker.task={task_id}",
            "--label", f"synai.broker.image-digest={self.config.image_digest.removeprefix('sha256:')}",
            "--label", f"synai.broker.snapshot-digest={self.registry.task(task_id)['snapshot_digest']}",
            "--user", "65532:65532",
            "--userns", "keep-id:uid=65532,gid=65532",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--security-opt", "seccomp=/usr/share/containers/seccomp.json",
            "--network", "none", "--ipc", "private",
            "--read-only", "--read-only-tmpfs=false",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,nr_inodes=4096,mode=1777",
            "--memory", str(self.config.profile.memory_bytes),
            "--cpus", str(self.config.profile.cpus),
            "--pids-limit", str(self.config.profile.pids),
            "--mount", f"type=bind,src={workspace},dst=/workspace,rw,rprivate,nodev,nosuid",
            "--workdir", "/workspace",
            self.config.image,
            "sleep", "infinity",
        )

    async def start_owned(self, task_id: str) -> None:
        lock = self._containment_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            self.registry.require_execution_authority()
            self._require_accepted_execution_source(task_id)
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
        return await self._inspect_record(
            task_id, owned, allow_unverified_workspace=True,
        )

    async def _inspect_record(
        self,
        task_id: str,
        owned: Any,
        *,
        allow_unverified_workspace: bool = False,
    ) -> dict[str, Any]:
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
            if self.workspace_resolver is None:
                if not allow_unverified_workspace:
                    raise BrokerError("workspace_slot_missing", "Task workspace resolver is unavailable.", 503)
            expected_workspace: str | None = None
            if self.workspace_resolver is not None:
                try:
                    expected_workspace = str(self.workspace_resolver(task_id))
                except BrokerError:
                    if not allow_unverified_workspace:
                        raise
            expected_image_id = self.image_id
            security_options = host.get("SecurityOpt", [])
            if (
                info["Id"] != owned["container_id"]
                or labels.get("synai.broker.allocation") != owned["allocation_id"]
                or labels.get("synai.broker.task") != task_id
                or labels.get("synai.broker.image-digest") != owned["image_digest"]
                or labels.get("synai.broker.snapshot-digest") != owned["snapshot_digest"]
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
                or (
                    expected_workspace is not None
                    and workspace_mounts[0].get("Source") != expected_workspace
                )
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
        lock = self._containment_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            return await self._terminate_container(task_id, recovery=False)

    async def terminate_recorded(self, task_id: str) -> str:
        lock = self._containment_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            return await self._terminate_container(task_id, recovery=True)

    def emergency_containment_available(self) -> bool:
        from synai.broker.quota_slots import _process_identity_is_restricted

        return (
            _process_identity_is_restricted(
                self.config.service_uid, self.config.service_gid,
            )
            and shutil.which(self.config.podman) is not None
        )

    async def contain_recorded(self, task_id: str) -> ContainmentResult:
        lock = self._containment_locks.setdefault(task_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=2)
        except TimeoutError:
            return ContainmentResult(
                "inspection_inconclusive", error_code="containment_busy",
            )
        try:
            return await self._contain_recorded_locked(task_id)
        finally:
            lock.release()

    async def _contain_recorded_locked(self, task_id: str) -> ContainmentResult:
        if not self.emergency_containment_available():
            return ContainmentResult(
                "runtime_unavailable", error_code="containment_authority_unavailable",
            )
        try:
            owned = self.registry.container_record(task_id)
            self._validate_containment_record(task_id, owned)
        except (BrokerError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            return ContainmentResult(
                "identity_mismatch",
                error_code=exc.code if isinstance(exc, BrokerError) else "ownership_record_unavailable",
            )

        first = await self._containment_inspect(task_id, owned)
        if isinstance(first, ContainmentResult):
            return first
        if first["State"]["Running"] is not True:
            return self._verified_stopped(first)

        try:
            await self._run(
                "stop", "--time", "3", owned["container_id"],
                timeout=5, maximum=4096,
            )
        except (BrokerError, OSError, TimeoutError):
            pass
        stopped = await self._containment_inspect(task_id, owned)
        if isinstance(stopped, ContainmentResult):
            return stopped
        if stopped["State"]["Running"] is not True:
            return self._verified_stopped(stopped)

        try:
            await self._run(
                "kill", owned["container_id"], timeout=5, maximum=4096,
            )
        except (BrokerError, OSError, TimeoutError):
            pass
        final = await self._containment_inspect(task_id, owned)
        if isinstance(final, ContainmentResult):
            return final
        if final["State"]["Running"] is True:
            return ContainmentResult("still_running", error_code="forced_stop_unconfirmed")
        return self._verified_stopped(final)

    def _validate_containment_record(self, task_id: str, owned: Any) -> None:
        container_id = None if owned is None else owned["container_id"]
        if (
            owned is None
            or owned["task_id"] != task_id
            or not re.fullmatch(r"[a-f0-9]{32}", task_id)
            or not isinstance(container_id, str)
            or len(container_id) != 64
            or any(char not in "0123456789abcdef" for char in container_id)
            or not re.fullmatch(r"[a-f0-9]{32}", str(owned["allocation_id"]))
            or owned["allocation_id"] != owned["task_allocation_id"]
            or owned["image_digest"] != self.config.image_digest.removeprefix("sha256:")
            or owned["task_image_digest"] != self.config.image_digest.removeprefix("sha256:")
            or owned["snapshot_digest"] != owned["task_snapshot_digest"]
            or not re.fullmatch(r"[a-f0-9]{64}", str(owned["snapshot_digest"]))
            or owned["resource_profile"] != self.config.profile.name
            or owned["task_profile"] != self.config.profile.name
            or owned["lifecycle_state"] not in {"owned", "terminated", "quarantined"}
        ):
            raise BrokerError(
                "container_ownership_invalid",
                "Durable container ownership identity is incomplete or inconsistent.",
                409,
            )

    async def _containment_inspect(
        self, task_id: str, owned: Any,
    ) -> dict[str, Any] | ContainmentResult:
        try:
            info = await self.inspect_recorded(task_id)
            self._verify_containment_info(task_id, owned, info)
            if type(info["State"].get("Running")) is not bool:
                return ContainmentResult(
                    "inspection_inconclusive", error_code="container_state_invalid",
                )
            return info
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            if exc.code == "container_policy_mismatch":
                return ContainmentResult("identity_mismatch", error_code=exc.code)
            if exc.code in {"container_inspection_invalid", "workspace_slot_missing"}:
                return ContainmentResult("inspection_inconclusive", error_code=exc.code)
            return await self._containment_absence_or_unavailable(owned, exc.code)
        except (OSError, TimeoutError, sqlite3.Error):
            return await self._containment_absence_or_unavailable(
                owned, "runtime_unavailable",
            )

    def _verify_containment_info(self, task_id: str, owned: Any, info: dict[str, Any]) -> None:
        try:
            labels = info["Config"]["Labels"]
            if (
                info["Id"] != owned["container_id"]
                or labels.get("synai.broker.task") != task_id
                or labels.get("synai.broker.allocation") != owned["allocation_id"]
                or labels.get("synai.broker.image-digest") != owned["image_digest"]
                or labels.get("synai.broker.snapshot-digest") != owned["snapshot_digest"]
                or self.image_id is None
                or info["Image"] != self.image_id
                or not isinstance(info["State"], dict)
            ):
                raise ValueError("Runtime identity differs from durable ownership")
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise BrokerError(
                "container_policy_mismatch",
                "Runtime container identity does not match its durable owner record.",
                409,
            ) from exc

    async def _containment_absence_or_unavailable(
        self, owned: Any, error_code: str,
    ) -> ContainmentResult:
        try:
            exists = await self._container_exists(owned["container_id"])
        except (BrokerError, OSError, TimeoutError):
            return ContainmentResult(
                "runtime_unavailable", error_code="container_state_unavailable",
            )
        if not exists:
            evidence = hashlib.sha256(
                f"{owned['container_id']}\n{owned['allocation_id']}\nverified_absent".encode(),
            ).hexdigest()
            return ContainmentResult("verified_absent", evidence=evidence)
        return ContainmentResult(
            "inspection_inconclusive", error_code=error_code,
        )

    @staticmethod
    def _verified_stopped(info: dict[str, Any]) -> ContainmentResult:
        raw = json.dumps(info, sort_keys=True, separators=(",", ":"))
        return ContainmentResult(
            "verified_stopped",
            evidence=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )

    async def _terminate_container(self, task_id: str, *, recovery: bool) -> str:
        if (
            self.storage_containment_check is not None
            and self.storage_containment_check() is not True
        ):
            raise BrokerError(
                "runtime_storage_unverified",
                "Podman containment storage roles are not currently verified.",
                503,
            )
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
        lock = self._containment_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            return await self._remove_owned_locked(task_id)

    async def _remove_owned_locked(self, task_id: str) -> str:
        owned = self.registry.owned_container(task_id)
        if owned["lifecycle_state"] != "terminated":
            await self._terminate_container(task_id, recovery=False)
            owned = self.registry.owned_container(task_id)
        await self.inspect_owned(task_id)
        await self._run("rm", owned["container_id"], timeout=10, maximum=4096)
        if await self._container_exists(owned["container_id"]):
            self.registry.mark_container(task_id, "quarantined")
            raise BrokerError(
                "container_removal_unconfirmed",
                "Podman still reports the owned task container after removal.",
                409,
            )
        evidence = hashlib.sha256(
            f"{owned['container_id']}\n{owned['allocation_id']}\nremoved".encode(),
        ).hexdigest()
        self.registry.mark_container(task_id, "removed", cleanup=evidence)
        self.registry.record_reconciliation(
            task_id, "removed", f"verified_absent:{evidence}",
        )
        return evidence

    async def verify_removed(self, task_id: str) -> None:
        owned = self.registry.container_record(task_id)
        if owned is None:
            return
        if not owned["container_id"]:
            raise BrokerError(
                "container_removal_unconfirmed",
                "Task container identity is incomplete and cannot be reconciled.",
                409,
            )
        if await self._container_exists(owned["container_id"]):
            self.registry.mark_container(task_id, "quarantined")
            raise BrokerError(
                "container_removal_unconfirmed",
                "Podman still reports the owned task container.",
                409,
            )
        evidence = hashlib.sha256(
            f"{owned['container_id']}\n{owned['allocation_id']}\nverified_absent".encode(),
        ).hexdigest()
        self.registry.record_reconciliation(
            task_id, "removed", f"verified_absent:{evidence}",
        )

    async def _container_exists(self, container_id: str) -> bool:
        executable = shutil.which(self.config.podman)
        if executable is None:
            raise BrokerError("runtime_unavailable", "Rootless Podman is unavailable.", 503)
        process = await asyncio.create_subprocess_exec(
            executable, "container", "exists", container_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={
                **self._runtime_environment(),
            },
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _read_bounded(process.stdout, 4096),
                    _read_bounded(process.stderr, 16 * 1024),
                ),
                timeout=10,
            )
            await process.wait()
        except (TimeoutError, asyncio.CancelledError, BrokerError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode == 0:
            return True
        if process.returncode == 1:
            return False
        raise BrokerError(
            "runtime_operation_failed",
            "Podman could not verify task-container removal.",
            502,
        )

    async def execute(
        self, task_id: str, argv: list[str], cwd: str, timeout: int, output_limit: int,
    ) -> dict[str, Any]:
        self.registry.require_execution_authority()
        self._require_accepted_execution_source(task_id)
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
                **self._runtime_environment(),
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

    def _require_accepted_execution_source(self, task_id: str) -> None:
        if self.workspace_authority is None:
            raise BrokerError("bind_source_unverified", "Verified workspace authority is unavailable.", 503)
        with self.workspace_authority(task_id) as source:
            source.revalidate()
        raise BrokerError(
            "bind_source_resolution_unaccepted",
            "Actual runtime mount-source identity requires later acceptance; execution is refused.",
            503,
        )

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
            env=self._runtime_environment(),
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

    def _runtime_environment(self) -> dict[str, str]:
        temporary = next(
            role for role in self.config.storage_roles
            if role.kind == "temporary"
        )
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.config.state_dir),
            "TMPDIR": str(temporary.path),
            "XDG_RUNTIME_DIR": str(temporary.path),
        }


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while len(result) <= limit:
        chunk = await stream.read(min(65536, limit + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
    raise BrokerError("operation_output_limit", "Task output exceeded its configured limit.", 413)
