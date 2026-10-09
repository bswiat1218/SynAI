from __future__ import annotations

import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from synai.config import Settings
from synai.coding_agent.memory import project_memory_id
from synai.execution_backend import validate_workspace
from synai.web.config import WebConfig
from synai.web.database import MetadataDatabase


@dataclass(frozen=True)
class HostWorkspacePath:
    canonical_path: Path


@dataclass(frozen=True)
class WebWorkspacePath:
    project_id: str


@dataclass(frozen=True)
class RunnerWorkspaceMount:
    container_path: PurePosixPath = PurePosixPath("/workspace")
    read_only: bool = True


@dataclass(frozen=True)
class WorkspaceIdentity:
    key: str
    canonical_path: Path
    device_id: int
    inode: int
    owner_uid: int
    project_memory_id: str


@dataclass(frozen=True)
class RegisteredProject:
    project_id: str
    name: str
    status: str
    host_path: HostWorkspacePath | None
    web_path: WebWorkspacePath
    runner_mount: RunnerWorkspaceMount
    identity: WorkspaceIdentity | None


class ProjectRegistryError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


class ProjectRegistry:
    def __init__(self, database: MetadataDatabase, config: WebConfig) -> None:
        self.database = database
        self.settings = Settings(
            history_dir=config.data_root, ollama_url=config.ollama_url,
        )
        self.mounts: dict[str, tuple[Path, WorkspaceIdentity]] = {}
        if len(config.workspace_mounts) > 256:
            raise ValueError("At most 256 workspace roots may be configured")
        for mount in config.workspace_mounts:
            path, identity = self._validate_configured_path(mount.host_path)
            self.mounts[mount.key] = path, identity

    def register(self, workspace_key: str) -> RegisteredProject:
        mount = self.mounts.get(workspace_key)
        if mount is None:
            raise ProjectRegistryError(
                "workspace_not_allowed", "Workspace is not in the operator allowlist.", 404,
            )
        path, _ = mount
        try:
            current_path, identity = self._validate_configured_path(path)
        except (OSError, ValueError) as exc:
            raise ProjectRegistryError(
                "workspace_unavailable", "Configured workspace is unavailable.", 409,
            ) from exc
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM projects WHERE workspace_key = ?", (workspace_key,),
                ).fetchone()
                if row is not None:
                    if not _same_identity(row, identity):
                        raise ProjectRegistryError(
                            "workspace_replaced",
                            "The configured workspace changed since registration.",
                            409,
                        )
                    connection.commit()
                    return self._project(row, current_path, identity, "active")
                project_id = secrets.token_hex(16)
                connection.execute(
                    "INSERT INTO projects(project_id, workspace_key, display_name, "
                    "device_id, inode, owner_uid, registered_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id, workspace_key, current_path.name[:255] or "Project",
                        identity.device_id, identity.inode, identity.owner_uid,
                        int(time.time()),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM projects WHERE project_id = ?", (project_id,),
                ).fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._project(row, current_path, identity, "active")

    def list_projects(self) -> tuple[RegisteredProject, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY registered_at, project_id LIMIT 256",
            ).fetchall()
        return tuple(self._current_project(row) for row in rows)

    def inspect(self, project_id: str) -> RegisteredProject:
        if not isinstance(project_id, str) or len(project_id) != 32:
            raise ProjectRegistryError("project_not_found", "Project was not found.", 404)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectRegistryError("project_not_found", "Project was not found.", 404)
        return self._current_project(row)

    def resolve_for_internal_use(self, project_id: str) -> RegisteredProject:
        project = self.inspect(project_id)
        if project.status != "active" or project.host_path is None or project.identity is None:
            raise ProjectRegistryError(
                "workspace_unavailable", "Registered workspace is unavailable.", 409,
            )
        return project

    def _current_project(self, row: object) -> RegisteredProject:
        project_row = row
        mount = self.mounts.get(project_row["workspace_key"])
        if mount is None:
            return self._project(project_row, None, None, "stale")
        configured_path, _ = mount
        try:
            path, identity = self._validate_configured_path(configured_path)
        except (OSError, ValueError):
            return self._project(project_row, None, None, "stale")
        if not _same_identity(project_row, identity):
            return self._project(project_row, None, None, "stale")
        return self._project(project_row, path, identity, "active")

    @staticmethod
    def _project(
        row: object,
        path: Path | None,
        identity: WorkspaceIdentity | None,
        status: str,
    ) -> RegisteredProject:
        project_id = row["project_id"]
        return RegisteredProject(
            project_id=project_id,
            name=row["display_name"],
            status=status,
            host_path=HostWorkspacePath(path) if path is not None else None,
            web_path=WebWorkspacePath(project_id),
            runner_mount=RunnerWorkspaceMount(),
            identity=identity,
        )

    def _validate_configured_path(self, configured_path: Path) -> tuple[Path, WorkspaceIdentity]:
        path = configured_path.expanduser().absolute()
        _reject_symlink_components(path)
        canonical = path.resolve(strict=True)
        if path != canonical:
            raise ValueError("Configured workspace path must already be canonical")
        workspace = validate_workspace(canonical, self.settings)
        descriptor = os.open(
            workspace,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            info = os.fstat(descriptor)
            path_info = os.stat(workspace, follow_symlinks=False)
            if (
                not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
            ):
                raise ValueError("Workspace directory identity changed during validation")
        finally:
            os.close(descriptor)
        identity_material = f"{os.getuid()}:{info.st_dev}:{info.st_ino}:{workspace}"
        return workspace, WorkspaceIdentity(
            key=identity_material,
            canonical_path=workspace,
            device_id=info.st_dev,
            inode=info.st_ino,
            owner_uid=info.st_uid,
            project_memory_id=project_memory_id(workspace),
        )


def _same_identity(row: object, identity: WorkspaceIdentity) -> bool:
    return (
        row["device_id"] == identity.device_id
        and row["inode"] == identity.inode
        and row["owner_uid"] == identity.owner_uid
    )


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Configured workspace paths cannot contain symlinks")
