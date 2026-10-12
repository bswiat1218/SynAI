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
from unittest.mock import AsyncMock, Mock, patch

from synai.broker.app import BrokerService
from synai.broker.config import BrokerReadiness
from synai.broker.filesystem import Directory
from synai.broker.protocol import BrokerError, sign_claim
from synai.broker.registry import BrokerRegistry
from synai.broker.quota_slots import QuotaSlotManager, aggregate_storage_reasons
from synai.broker.quota_slots import _Mount
from synai.broker.runtime import ContainmentResult, RootlessPodmanRuntime
from synai.broker.runtime_storage import PATH_ROLES, RuntimeStorageLayout, RuntimeStoragePolicy
from synai.broker.storage_inspector import StorageObservation, inventory_generation
from synai.broker.filesystem import mount_identity
from tests.test_broker import CLAIM_PRIVATE, FakeQuotaManager, claim, config


def encode_toml(document: dict[str, object]) -> bytes:
    lines = []
    for table, settings in document.items():
        assert isinstance(settings, dict)
        lines.append(f"[{table}]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in settings.items())
    return ("\n".join(lines) + "\n").encode()


def provision_fixture(selected):
    policy = RuntimeStoragePolicy(selected)
    digests = []
    for name, document in zip(
        ("runtime-storage.conf", "runtime-containers.conf"), policy.expected_configuration(),
    ):
        payload = encode_toml(document)
        path = selected.state_dir / name
        path.write_bytes(payload)
        path.chmod(0o400)
        digests.append(hashlib.sha256(payload).hexdigest())
    return replace(selected, runtime_storage_config_digest=digests[0],
                   runtime_containers_config_digest=digests[1])


def observation(policy):
    return {
        "paths": {purpose: str(path) for purpose, path in policy.paths().items()},
        "driver": "overlay", "log_driver": "none", "event_logger": "none",
        "storage_config_digest": policy.config.runtime_storage_config_digest,
        "containers_config_digest": policy.config.runtime_containers_config_digest,
    }


def quota_observations(selected):
    return {
        role.role_id: StorageObservation(
            role.role_id, "a" * 32, inventory_generation(selected.storage_roles),
            int(time.time()), role.filesystem_uuid, str(role.mount_path), 10,
            "ext4", "project", True, role.project_id, True,
            role.byte_limit, role.inode_limit, 0, 0,
            selected.quota_volume_bytes, selected.quota_volume_bytes,
            selected.quota_volume_inodes, selected.quota_volume_inodes,
            role.owner_uid, role.owner_gid,
        )
        for role in selected.storage_roles
    }


class RuntimeStoragePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.selected = provision_fixture(config(Path(self.temporary.name) / "broker", slot_count=4))
        self.policy = RuntimeStoragePolicy(self.selected)
        self.authority = lambda _: Directory(self.selected.state_dir)

    def test_valid_configured_policy_and_shared_project_domains(self):
        self.selected.validate()
        self.policy.verify_configuration(self.authority)
        observed = observation(self.policy)
        validator = Mock()
        parsed = self.policy.verify_observation(observed, validator)
        self.assertIsInstance(parsed, RuntimeStorageLayout)
        self.assertEqual(validator.call_count, len(PATH_ROLES))
        self.assertIsNone(self.policy.failure_code)
        self.assertEqual(aggregate_storage_reasons(self.selected, quota_observations(self.selected)), ())

    def test_unapproved_configuration_is_refused_without_creation(self):
        policy = RuntimeStoragePolicy(replace(self.selected, runtime_storage_config_digest=None))
        with self.assertRaises(BrokerError) as denied:
            policy.verify_configuration(self.authority)
        self.assertEqual(denied.exception.code, "runtime_storage_configuration_unapproved")

    def test_configuration_digests_and_unexpected_settings_are_rejected(self):
        path = self.selected.state_dir / "runtime-storage.conf"
        path.chmod(0o600)
        payload = path.read_bytes() + b'imagestore = "/var/tmp/unbounded"\n'
        path.write_bytes(payload)
        path.chmod(0o400)
        with self.assertRaises(BrokerError):
            self.policy.verify_configuration(self.authority)
        changed = replace(self.selected, runtime_storage_config_digest=hashlib.sha256(payload).hexdigest())
        with self.assertRaises(BrokerError) as denied:
            RuntimeStoragePolicy(changed).verify_configuration(self.authority)
        self.assertEqual(denied.exception.code, "runtime_storage_configuration_conflict")

    def test_configuration_replacement_and_symlinks_are_rejected(self):
        self.policy.verify_configuration(self.authority)
        path = self.selected.state_dir / "runtime-storage.conf"
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.write_bytes(saved.read_bytes())
        path.chmod(0o400)
        with self.assertRaises(BrokerError):
            self.policy.verify_configuration(self.authority)
        path.unlink()
        path.symlink_to(saved)
        with self.assertRaises((BrokerError, OSError)):
            self.policy.verify_configuration(self.authority)

    def test_mutable_configuration_is_refused(self):
        (self.selected.state_dir / "runtime-containers.conf").chmod(0o600)
        with self.assertRaises(BrokerError) as denied:
            self.policy.verify_configuration(self.authority)
        self.assertEqual(denied.exception.code, "runtime_storage_configuration_mutable")

    def test_environment_is_complete_and_does_not_inherit_overrides(self):
        with patch.dict(os.environ, {
            "TMPDIR": "/var/tmp", "STORAGE_DRIVER": "vfs",
            "STORAGE_OPTS": "mount_program=/evil", "CONTAINERS_CONF_OVERRIDE": "/evil",
            "CONTAINER_HOST": "unix:///evil", "CONTAINERS_STORAGE_CONF": "/evil",
        }):
            environment = self.policy.environment()
        self.assertEqual(environment["TMPDIR"], str(self.policy.paths()["image_copy_tmp"]))
        self.assertEqual(environment["XDG_RUNTIME_DIR"], str(self.policy.paths()["run_root"]))
        for key in ("STORAGE_DRIVER", "STORAGE_OPTS", "CONTAINERS_CONF_OVERRIDE", "CONTAINER_HOST"):
            self.assertNotIn(key, environment)

    def test_each_resolved_path_drift_is_rejected(self):
        for purpose in PATH_ROLES:
            with self.subTest(purpose=purpose):
                value = observation(self.policy)
                value["paths"][purpose] = "/var/tmp/unbounded"
                with self.assertRaises(BrokerError):
                    self.policy.verify_observation(value, Mock())

    def test_missing_malformed_and_unknown_metadata_are_rejected(self):
        for value in (None, {}, [], {"paths": {}}, {**observation(self.policy), "unexpected": "/host"}):
            with self.subTest(value=value):
                with self.assertRaises(BrokerError):
                    RuntimeStorageLayout.parse(value)
        for text in ("relative", "/var/../tmp", "/var//tmp", "/var/tmp/", "/var/\x00tmp"):
            value = observation(self.policy)
            value["paths"]["run_root"] = text
            with self.assertRaises(BrokerError):
                RuntimeStorageLayout.parse(value)

    def test_driver_and_logging_overrides_are_rejected(self):
        for field, values in (
            ("driver", ("vfs", "btrfs", "zfs")),
            ("log_driver", ("journald", "k8s-file", "json-file")),
            ("event_logger", ("journald", "file")),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(BrokerError):
                        RuntimeStorageLayout.parse({**observation(self.policy), field: value})

    def test_descriptor_identity_failure_is_not_converted_to_passing_evidence(self):
        check = Mock(side_effect=BrokerError("filesystem_identity_changed", "substitution", 503))
        with self.assertRaises(BrokerError):
            self.policy.verify_observation(observation(self.policy), check)
        self.assertIsNone(self.policy.observed)

    def container(self):
        self.policy.verify_observation(observation(self.policy), Mock())
        graph = self.policy.paths()["graph_root"]
        run = self.policy.paths()["run_root"]
        return {
            "GraphDriver": {"Name": "overlay", "Data": {
                "UpperDir": str(graph / "overlay/id/diff"), "WorkDir": str(graph / "overlay/id/work"),
                "MergedDir": str(graph / "overlay/id/merged"), "LowerDir": str(graph / "overlay/layer/diff"),
            }},
            "StaticDir": str(graph / "overlay-containers/id/userdata"),
            "ResolvConfPath": str(graph / "overlay-containers/id/userdata/resolv.conf"),
            "HostnamePath": str(graph / "overlay-containers/id/userdata/hostname"),
            "HostsPath": str(graph / "overlay-containers/id/userdata/hosts"),
            "OCIConfigPath": str(run / "id/config.json"),
            "HostConfig": {"LogConfig": {"Type": "none", "Config": {}}}, "LogPath": "",
            "Mounts": [{"Type": "tmpfs", "Destination": "/tmp", "Options": [
                "rw", "noexec", "nosuid", "nodev", "size=65536k", "nr_inodes=4096", "mode=1777",
            ]}],
        }

    def checked_path(self, role_id, path):
        role = next(role for role in self.selected.storage_roles if role.role_id == role_id)
        if not path.is_relative_to(role.path) or ".." in path.parts:
            raise BrokerError("runtime_storage_path_escape", "path escape", 503)

    def test_complete_container_fixture_accounts_for_layers_and_metadata(self):
        self.policy.verify_container(self.container(), self.checked_path)

    def test_overlay_and_writable_layer_escape_is_rejected(self):
        for key in ("UpperDir", "WorkDir", "LowerDir", "MergedDir"):
            with self.subTest(key=key):
                info = self.container()
                info["GraphDriver"]["Data"][key] = "/var/tmp/escaped"
                with self.assertRaises(BrokerError):
                    self.policy.verify_container(info, self.checked_path)

    def test_container_metadata_paths_cannot_escape(self):
        for key in ("StaticDir", "OCIConfigPath", "HostsPath", "ResolvConfPath", "HostnamePath"):
            with self.subTest(key=key):
                info = self.container()
                info[key] = "/var/tmp/metadata"
                with self.assertRaises(BrokerError):
                    self.policy.verify_container(info, self.checked_path)

    def test_container_external_volume_and_unbounded_logs_are_rejected(self):
        info = self.container()
        for bad in (
            {**info, "LogPath": "/var/log/container.log"},
            {**info, "HostConfig": {"LogConfig": {"Type": "journald", "Config": {}}}},
            {**info, "Mounts": [{"Type": "volume", "Source": "/host"}]},
        ):
            with self.assertRaises(BrokerError):
                self.policy.verify_container(bad, self.checked_path)

    def test_unknown_container_metadata_and_unbounded_tmpfs_are_rejected(self):
        for key in ("GraphDriver", "StaticDir", "OCIConfigPath", "LogPath", "Mounts"):
            bad = self.container()
            del bad[key]
            with self.assertRaises(BrokerError):
                self.policy.verify_container(bad, self.checked_path)
        info = self.container()
        info["Mounts"][0]["Options"].remove("nr_inodes=4096")
        with self.assertRaises(BrokerError):
            self.policy.verify_container(info, self.checked_path)

    def test_root_backed_and_overlapping_domains_are_rejected(self):
        for kind in ("podman_graph", "podman_runtime"):
            roles = tuple(replace(role, mount_path=Path("/")) if role.kind == kind else role
                          for role in self.selected.storage_roles)
            with self.assertRaises(ValueError):
                replace(self.selected, storage_roles=roles).validate()
        graph = next(role for role in self.selected.storage_roles if role.kind == "podman_graph")
        roles = tuple(replace(role, path=graph.path / "run") if role.kind == "podman_runtime" else role
                      for role in self.selected.storage_roles)
        with self.assertRaises(ValueError):
            replace(self.selected, storage_roles=roles).validate()

    def test_conflicting_project_domains_and_wrong_quota_are_rejected(self):
        baseline = quota_observations(self.selected)
        role = self.selected.storage_roles[0]
        for changes in ({"project_id": role.project_id + 1}, {"byte_limit": role.byte_limit - 1},
                        {"inode_limit": role.inode_limit - 1}, {"mount_target": "/"},
                        {"filesystem_type": "xfs"}, {"owner_uid": role.owner_uid + 1}):
            changed = {**baseline, role.role_id: replace(baseline[role.role_id], **changes)}
            self.assertTrue(aggregate_storage_reasons(self.selected, changed))
        roles = list(self.selected.storage_roles)
        roles[1] = replace(roles[1], project_id=roles[0].project_id)
        self.assertEqual(aggregate_storage_reasons(replace(self.selected, storage_roles=tuple(roles)), baseline),
                         ("storage_quota_domain_conflict",))

    def test_reserve_includes_remaining_entitlements_and_operational_overhead(self):
        baseline = quota_observations(self.selected)
        remaining_bytes = sum(role.byte_limit for role in self.selected.storage_roles)
        remaining_inodes = sum(role.inode_limit for role in self.selected.storage_roles)
        for field, sufficient in (
            ("filesystem_bytes_available", remaining_bytes + self.selected.quota_reserve_bytes + 256 * 1024**2),
            ("filesystem_inodes_available", remaining_inodes + self.selected.quota_reserve_inodes + 4096),
        ):
            changed = {key: replace(value, **{field: sufficient}) for key, value in baseline.items()}
            self.assertEqual(aggregate_storage_reasons(self.selected, changed), ())
            key = next(iter(changed))
            changed[key] = replace(changed[key], **{field: sufficient - 1})
            self.assertEqual(aggregate_storage_reasons(self.selected, changed), ("storage_aggregate_headroom",))
        used = {key: replace(value, bytes_used=1024, inodes_used=1) for key, value in baseline.items()}
        self.assertEqual(aggregate_storage_reasons(self.selected, used), ())

    def test_minimum_reserve_cannot_be_relaxed(self):
        for changes in ({"quota_reserve_bytes": 0}, {"quota_reserve_inodes": 0}):
            with self.assertRaises(ValueError):
                replace(self.selected, **changes).validate()

    def test_runtime_descendant_project_and_mount_verification(self):
        role = next(role for role in self.selected.storage_roles if role.kind == "podman_graph")
        child = role.path / "child"
        child.mkdir(mode=0o700)
        manager = QuotaSlotManager(self.selected, Mock())
        manager.open_storage_role = lambda _: Directory(role.path)
        with patch("synai.broker.quota_slots._project_attributes", return_value=(role.project_id, 0x200)):
            manager.verify_runtime_path(role.role_id, child)
            child.rename(role.path / "old")
            child.mkdir(mode=0o700)
            with self.assertRaises(BrokerError):
                manager.verify_runtime_path(role.role_id, child)
        with patch("synai.broker.quota_slots._project_attributes", return_value=(role.project_id + 1, 0x200)):
            with self.assertRaises(BrokerError):
                manager.verify_runtime_path(role.role_id, child)
        child.rmdir()
        child.symlink_to(role.path / "old")
        with self.assertRaises((BrokerError, OSError)):
            manager.verify_runtime_path(role.role_id, child)

    def test_runtime_descendant_unexpected_mount_is_rejected(self):
        role = next(role for role in self.selected.storage_roles if role.kind == "podman_graph")
        child = role.path / "mounted"
        child.mkdir(mode=0o700)
        inode = child.stat().st_ino
        manager = QuotaSlotManager(self.selected, Mock())
        manager.open_storage_role = lambda _: Directory(role.path)

        def identity(descriptor):
            device, current, mount = mount_identity(descriptor)
            return device, current, mount + 1 if current == inode else mount

        with (
            patch("synai.broker.quota_slots._project_attributes", return_value=(role.project_id, 0x200)),
            patch("synai.broker.quota_slots.mount_identity", side_effect=identity),
        ):
            with self.assertRaises(BrokerError):
                manager.verify_runtime_path(role.role_id, child)

    def test_root_backing_cannot_be_hidden_by_an_alternate_mount_target(self):
        role = next(role for role in self.selected.storage_roles if role.kind == "podman_runtime")
        approved = _Mount(10, "8:1", "/", str(role.mount_path),
                          frozenset({"prjquota"}), "ext4", frozenset({"prjquota"}))
        host_root = replace(approved, mount_id=1, target="/")
        manager = QuotaSlotManager(self.selected, Mock())
        with patch("synai.broker.quota_slots._mount_for_fd", side_effect=[approved, approved, host_root]):
            with self.assertRaises(ValueError):
                manager._verify_local_role_path(role, quota_observations(self.selected)[role.role_id])


class RuntimeStorageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.selected = provision_fixture(config(Path(self.temporary.name) / "broker"))
        self.service = BrokerService(self.selected)
        self.service.quota_manager = FakeQuotaManager(self.selected, self.service.registry)
        self.service.registry.initialize(storage_verified=True, provision=True)
        self.runtime = self.service.runtime
        self.runtime.storage_path_verifier = Mock()
        self.runtime.storage_policy.verify_observation(observation(self.runtime.storage_policy), Mock())

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    async def test_no_real_subprocess_or_mock_can_enable_storage_acceptance(self):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as launch:
            for attempt in (
                self.runtime._run("info", timeout=1, maximum=1024),
                self.runtime._container_exists("a" * 64),
            ):
                with self.assertRaises(BrokerError) as denied:
                    await attempt
                self.assertEqual(denied.exception.code, "runtime_storage_acceptance_pending")
            launch.assert_not_awaited()
        self.runtime._run = AsyncMock(side_effect=BrokerError("unaccepted", "unaccepted", 503))
        readiness = await self.runtime.verify_startup(BrokerReadiness(False, False, {"podman_available": True}, ()))
        self.assertFalse(readiness.execution_available)
        self.assertIn("runtime_storage_acceptance_pending", readiness.reasons)

    async def test_every_runtime_lifecycle_command_retains_launch_refusal(self):
        commands = (
            ("info", "--format", "json"),
            ("image", "inspect", self.selected.image),
            ("unshare", "cat", "/proc/self/uid_map"),
            ("create", self.selected.image),
            ("start", "a" * 64),
            ("exec", "a" * 64, "false"),
            ("inspect", "a" * 64),
            ("container", "exists", "a" * 64),
            ("stop", "--time", "3", "a" * 64),
            ("kill", "a" * 64),
            ("rm", "a" * 64),
        )
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as launch:
            for arguments in commands:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(BrokerError) as denied:
                        await self.runtime._run(*arguments, timeout=1, maximum=1024)
                    self.assertEqual(denied.exception.code, "runtime_storage_acceptance_pending")
            launch.assert_not_awaited()

    async def test_missing_runtime_observer_and_restart_drift_fail_closed(self):
        ready, reasons = await self.runtime.verify_storage_assurance()
        self.assertFalse(ready)
        self.assertIn("runtime_storage_observation_missing", reasons)
        self.runtime.storage_observer = AsyncMock(return_value=observation(self.runtime.storage_policy))
        self.assertFalse((await self.runtime.verify_storage_assurance())[0])
        fresh = RootlessPodmanRuntime(self.selected, self.service.registry)
        self.assertFalse((await fresh.verify_storage_assurance())[0])

    async def test_runtime_drift_watchdog_quarantines_active_task_and_preserves_unresolved_containment(self):
        request = claim("prepare", {})
        self.service.registry.authorize(request, sign_claim(request, CLAIM_PRIVATE))
        self.service.registry.transition(request.task_id, "ready")
        self.service.registry.reserve_container(request.task_id, request.allocation_id)
        self.service.registry.bind_container(request.task_id, request.allocation_id, "b" * 64)
        self.service.readiness = BrokerReadiness(False, False, {}, ("runtime_storage_acceptance_pending",))
        changed = observation(self.runtime.storage_policy)
        changed["paths"]["run_root"] = "/var/tmp/drift"
        self.runtime.storage_observer = AsyncMock(return_value=changed)
        self.runtime.contain_recorded = AsyncMock(return_value=ContainmentResult(
            "runtime_unavailable", error_code="runtime_storage_acceptance_pending",
        ))
        self.service._start_storage_watchdog()
        async with asyncio.timeout(4):
            while "storage_containment_unconfirmed" not in self.service.readiness.reasons:
                await asyncio.sleep(0.01)
        self.runtime.contain_recorded.assert_awaited_once_with(request.task_id)
        self.assertEqual(self.service.registry.container_record(request.task_id)["lifecycle_state"], "quarantined")
        self.assertEqual(self.service.registry.task_slot(request.task_id, allow_quarantined=True)["state"], "quarantined")
        self.runtime.storage_observer.return_value = observation(self.runtime.storage_policy)
        self.assertFalse((await self.runtime.verify_storage_assurance())[0])
        self.assertFalse(self.service.readiness.execution_available)

    async def test_runtime_observation_timeout_is_explicit(self):
        async def inspect():
            raise TimeoutError("controlled timeout")
        self.runtime.storage_observer = inspect
        ready, reasons = await self.runtime.verify_storage_assurance()
        self.assertFalse(ready)
        self.assertEqual(reasons, ("runtime_storage_observation_unavailable",))

    async def test_native_storage_report_requires_complete_observer_evidence(self):
        paths = self.runtime.storage_policy.paths()
        store = {
            "graphRoot": str(paths["graph_root"]), "runRoot": str(paths["run_root"]),
            "imageCopyTmpDir": str(paths["image_copy_tmp"]), "volumePath": str(paths["volume_path"]),
            "graphDriverName": "overlay", "graphOptions": {},
            "configFile": self.runtime._runtime_environment()["CONTAINERS_STORAGE_CONF"],
            "runtimePaths": observation(self.runtime.storage_policy),
        }
        self.assertTrue(self.runtime._verify_reported_storage_layout(store))
        for changes in (
            {"graphOptions": {"overlay.mount_program": "/evil"}},
            {"graphDriverName": "vfs"}, {"volumePath": "/host"},
            {"runtimePaths": None}, {"configFile": "/etc/containers/storage.conf"},
            {"additionalImageStores": ["/host"]}, {"newStoragePath": "/host"},
            {"runRoot": "/run/user/1000"}, {"imageCopyTmpDir": "/var/tmp"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.runtime._verify_reported_storage_layout({**store, **changes}))
        self.assertFalse((await self.runtime.verify_storage_assurance())[0])

    async def test_registry_seal_binds_approved_runtime_configuration(self):
        changed = replace(self.selected, runtime_storage_config_digest="e" * 64)
        registry = BrokerRegistry(changed)
        try:
            with self.assertRaises(BrokerError):
                registry.initialize(storage_verified=True)
        finally:
            registry.close()

    async def test_completely_passing_runtime_mocks_still_cannot_enable_execution(self):
        paths = self.runtime.storage_policy.paths()
        store = {
            "graphRoot": str(paths["graph_root"]), "runRoot": str(paths["run_root"]),
            "imageCopyTmpDir": str(paths["image_copy_tmp"]), "volumePath": str(paths["volume_path"]),
            "graphDriverName": "overlay", "graphOptions": {},
            "configFile": self.runtime._runtime_environment()["CONTAINERS_STORAGE_CONF"],
            "runtimePaths": observation(self.runtime.storage_policy),
        }

        async def run(*args, **kwargs):
            if args[0] == "info":
                return json.dumps({"host": {
                    "security": {"rootless": True, "seccompEnabled": True},
                    "cgroupVersion": "v2", "apparmorEnabled": True,
                }, "store": store})
            if args[0] == "unshare":
                return "0 1000 1\n1 100000 65536\n"
            return f"{self.selected.image_digest} image-id"

        with (
            patch.object(self.runtime, "_run", side_effect=run),
            patch("synai.broker.runtime.Path.is_file", return_value=True),
            patch("synai.broker.runtime.Path.exists", return_value=True),
        ):
            readiness = await self.runtime.verify_startup(
                BrokerReadiness(False, False, {"podman_available": True}, ()),
            )
        self.assertTrue(readiness.checks["storage_layout"])
        self.assertTrue(readiness.checks["runtime_storage_configured"])
        self.assertFalse(readiness.checks["runtime_storage_accepted"])
        self.assertFalse(readiness.execution_available)
        self.assertEqual(readiness.reasons, ("runtime_storage_acceptance_pending",))

    async def test_interrupted_runtime_worker_is_joined_despite_repeat_cancellation(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def check():
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("fixture barrier timed out")
            finished.set()

        worker = asyncio.create_task(self.runtime._storage_worker(check))
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        try:
            worker.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        self.assertTrue(finished.is_set())
