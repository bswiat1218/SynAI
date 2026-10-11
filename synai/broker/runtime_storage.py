from __future__ import annotations

import hashlib
import os
import stat
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from synai.broker.config import BrokerConfig
from synai.broker.filesystem import Directory
from synai.broker.protocol import BrokerError


# Distinct purposes can share a quota domain, but never add a second entitlement.
PATH_ROLES = {
    "graph_root": "podman_graph",
    "image_store": "podman_graph",
    "volume_path": "podman_graph",
    "static_dir": "podman_graph",
    "network_config": "podman_graph",
    "run_root": "podman_runtime",
    "engine_tmp": "podman_runtime",
    "xdg_runtime": "podman_runtime",
    "image_copy_tmp": "image_scratch",
    "process_tmp": "temporary",
    "cache": "temporary",
    "home": "broker_state",
    "config_home": "broker_state",
}


def unavailable(code: str) -> BrokerError:
    return BrokerError(code, "Runtime storage authority is unavailable or inconsistent.", 503)


@dataclass(frozen=True)
class RuntimeStorageLayout:
    paths: tuple[tuple[str, Path], ...]
    driver: str
    log_driver: str
    event_logger: str
    storage_config_digest: str
    containers_config_digest: str

    @classmethod
    def parse(cls, value: object) -> RuntimeStorageLayout:
        if not isinstance(value, dict) or set(value) != {
            "paths", "driver", "log_driver", "event_logger",
            "storage_config_digest", "containers_config_digest",
        }:
            raise unavailable("runtime_storage_metadata_invalid")
        paths = value["paths"]
        if not isinstance(paths, dict) or set(paths) != set(PATH_ROLES):
            raise unavailable("runtime_storage_paths_unknown")
        parsed = []
        for purpose, text in sorted(paths.items()):
            if not isinstance(text, str) or not text or "\x00" in text:
                raise unavailable("runtime_storage_metadata_invalid")
            path = Path(text)
            if not path.is_absolute() or text != str(path) or path != Path(os.path.normpath(text)):
                raise unavailable("runtime_storage_path_noncanonical")
            parsed.append((purpose, path))
        if value["driver"] != "overlay" or value["log_driver"] != "none" or value["event_logger"] != "none":
            raise unavailable("runtime_storage_driver_unsupported")
        digests = (value["storage_config_digest"], value["containers_config_digest"])
        if any(not isinstance(item, str) or len(item) != 64 or any(c not in "0123456789abcdef" for c in item)
               for item in digests):
            raise unavailable("runtime_storage_metadata_invalid")
        return cls(tuple(parsed), "overlay", "none", "none", *digests)


class RuntimeStoragePolicy:
    """Configured policy is not observed layout or live container acceptance."""

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.observed: RuntimeStorageLayout | None = None
        self._configuration_identities: dict[str, tuple[int, int]] = {}
        self.failure_code: str | None = None

    def paths(self) -> dict[str, Path]:
        roles = {role.kind: role.path for role in self.config.storage_roles}
        if not set(PATH_ROLES.values()) <= set(roles):
            raise unavailable("runtime_storage_inventory_missing")
        return {
            "graph_root": roles["podman_graph"],
            "image_store": roles["podman_graph"],
            "volume_path": roles["podman_graph"] / "volumes",
            "static_dir": roles["podman_graph"] / "libpod",
            "network_config": roles["podman_graph"] / "networks",
            "run_root": roles["podman_runtime"],
            "engine_tmp": roles["podman_runtime"] / "libpod" / "tmp",
            "xdg_runtime": roles["podman_runtime"],
            "image_copy_tmp": roles["image_scratch"],
            "process_tmp": roles["temporary"],
            "cache": roles["temporary"],
            "home": roles["broker_state"],
            "config_home": roles["broker_state"],
        }

    def environment(self) -> dict[str, str]:
        paths = self.paths()
        return {
            "PATH": "/usr/bin:/bin", "HOME": str(paths["home"]),
            "TMPDIR": str(paths["image_copy_tmp"]),
            "TMP": str(paths["process_tmp"]), "TEMP": str(paths["process_tmp"]),
            "XDG_RUNTIME_DIR": str(paths["xdg_runtime"]),
            "XDG_CONFIG_HOME": str(paths["config_home"]),
            "XDG_DATA_HOME": str(paths["graph_root"]),
            "XDG_CACHE_HOME": str(paths["cache"]),
            "CONTAINERS_STORAGE_CONF": str(paths["home"] / "runtime-storage.conf"),
            "CONTAINERS_CONF": str(paths["home"] / "runtime-containers.conf"),
        }

    def expected_configuration(self) -> tuple[dict[str, object], dict[str, object]]:
        paths = self.paths()
        return (
            {"storage": {
                "driver": "overlay", "graphroot": str(paths["graph_root"]),
                "runroot": str(paths["run_root"]),
                "rootless_storage_path": str(paths["graph_root"]),
                "transient_store": False,
            }},
            {
                "containers": {"log_driver": "none", "volumes": [], "mounts": []},
                "engine": {
                    "tmp_dir": str(paths["engine_tmp"]), "static_dir": str(paths["static_dir"]),
                    "volume_path": str(paths["volume_path"]),
                    "image_copy_tmp_dir": str(paths["image_copy_tmp"]),
                    "events_logger": "none", "hooks_dir": [],
                },
                "network": {"network_config_dir": str(paths["network_config"])},
            },
        )

    def verify_configuration(self, authority: Callable[[str], Directory]) -> None:
        state = next((role for role in self.config.storage_roles if role.kind == "broker_state"), None)
        if state is None:
            raise unavailable("runtime_storage_inventory_missing")
        expected = self.expected_configuration()
        for name, digest, document in zip(
            ("runtime-storage.conf", "runtime-containers.conf"),
            (self.config.runtime_storage_config_digest, self.config.runtime_containers_config_digest),
            expected,
        ):
            if digest is None:
                raise unavailable("runtime_storage_configuration_unapproved")
            with authority(state.role_id) as directory:
                descriptor = directory.open(name, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    info = os.fstat(descriptor)
                    identity = (info.st_dev, info.st_ino)
                    if (
                        not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222
                        or info.st_uid != self.config.service_uid or info.st_gid != self.config.service_gid
                    ):
                        raise unavailable("runtime_storage_configuration_mutable")
                    if self._configuration_identities.setdefault(name, identity) != identity:
                        raise unavailable("runtime_storage_configuration_replaced")
                    if info.st_size > 64 * 1024:
                        raise unavailable("runtime_storage_configuration_invalid")
                    payload = b""
                    while len(payload) <= 64 * 1024:
                        chunk = os.read(descriptor, 64 * 1024 + 1 - len(payload))
                        if not chunk:
                            break
                        payload += chunk
                    if len(payload) > 64 * 1024:
                        raise unavailable("runtime_storage_configuration_invalid")
                    current = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != identity:
                        raise unavailable("runtime_storage_configuration_replaced")
                finally:
                    os.close(descriptor)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise unavailable("runtime_storage_configuration_digest")
            try:
                parsed = tomllib.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise unavailable("runtime_storage_configuration_invalid") from exc
            if parsed != document or (
                "storage" in document and parsed["storage"]["transient_store"] is not False
            ):
                raise unavailable("runtime_storage_configuration_conflict")

    def verify_observation(self, value: object, verify_path: Callable[[str, Path], None]) -> RuntimeStorageLayout:
        layout = RuntimeStorageLayout.parse(value)
        if (
            dict(layout.paths) != self.paths()
            or layout.storage_config_digest != self.config.runtime_storage_config_digest
            or layout.containers_config_digest != self.config.runtime_containers_config_digest
        ):
            raise unavailable("runtime_storage_layout_mismatch")
        roles = {role.kind: role.role_id for role in self.config.storage_roles}
        for purpose, path in layout.paths:
            verify_path(roles[PATH_ROLES[purpose]], path)
        if self.observed is not None and self.observed != layout:
            raise unavailable("runtime_storage_layout_drift")
        self.observed = layout
        return layout

    def verify_container(self, info: object, verify_path: Callable[[str, Path], None]) -> None:
        if self.observed is None:
            raise unavailable("runtime_storage_observation_missing")
        if not isinstance(info, dict):
            raise unavailable("runtime_container_storage_unknown")
        try:
            graph = info["GraphDriver"]
            if graph["Name"] != "overlay" or set(graph["Data"]) != {
                "UpperDir", "WorkDir", "MergedDir", "LowerDir",
            }:
                raise ValueError("Unknown layer storage")
            graph_role = next(role for role in self.config.storage_roles if role.kind == "podman_graph")
            # MergedDir is an overlay mount, not an ext4 quota directory.
            merged = Path(graph["Data"]["MergedDir"])
            if not merged.is_relative_to(graph_role.path) or ".." in merged.parts:
                raise ValueError("Unexpected merged layer")
            layer_paths = [graph["Data"]["UpperDir"], graph["Data"]["WorkDir"]]
            lower = graph["Data"]["LowerDir"]
            if not isinstance(lower, str) or not lower:
                raise ValueError("Missing image layer storage")
            layer_paths.extend(lower.split(":"))
            layer_paths.extend(info[key] for key in ("StaticDir", "ResolvConfPath", "HostnamePath", "HostsPath"))
            run_role = next(role for role in self.config.storage_roles if role.kind == "podman_runtime")
            verify_path(run_role.role_id, Path(info["OCIConfigPath"]))
            for text in layer_paths:
                if not isinstance(text, str) or not text:
                    raise ValueError("Missing storage path")
                verify_path(graph_role.role_id, Path(text))
            if info["HostConfig"]["LogConfig"] != {"Type": "none", "Config": {}} or info["LogPath"]:
                raise ValueError("Runtime logs are not disabled")
            if any(mount.get("Type") == "volume" for mount in info["Mounts"]):
                raise ValueError("External volumes are unsupported")
            temporary = [mount for mount in info["Mounts"]
                         if mount.get("Type") == "tmpfs" and mount.get("Destination") == "/tmp"]
            if len(temporary) != 1 or set(temporary[0]["Options"]) != {
                "rw", "noexec", "nosuid", "nodev", "size=65536k", "nr_inodes=4096", "mode=1777",
            }:
                raise ValueError("Container tmpfs accounting is unknown")
        except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as exc:
            raise unavailable("runtime_container_storage_unknown") from exc
