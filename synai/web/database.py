from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

from synai.storage import ConversationStorage, checked_path


class MetadataDatabase:
    """Private, versioned browser-service metadata, separate from conversation history."""

    SCHEMA_VERSION = 2

    def __init__(self, data_root: Path) -> None:
        self.storage = ConversationStorage(data_root)
        self.directory = checked_path(self.storage.root / "web")
        self.path = checked_path(self.directory / "metadata.sqlite3")

    def initialize(self) -> None:
        self.storage.initialize()
        self._private_directory(self.directory)
        if self.path.exists() or self.path.is_symlink():
            checked_path(self.path)
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Web metadata must be a private regular file owned by this user")
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, self.SCHEMA_VERSION}:
                raise ValueError("Unsupported web metadata schema version")
            if version == 0:
                connection.executescript(
                    """
                    BEGIN EXCLUSIVE;
                    CREATE TABLE credentials (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        password_hash TEXT NOT NULL
                    );
                    CREATE TABLE sessions (
                        token_hash TEXT PRIMARY KEY,
                        csrf_hash TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE INDEX sessions_expiry ON sessions(expires_at);
                    CREATE TABLE login_limits (
                        peer_hash TEXT PRIMARY KEY,
                        window_start INTEGER NOT NULL,
                        attempts INTEGER NOT NULL,
                        blocked_until INTEGER NOT NULL
                    );
                    CREATE INDEX login_limits_age ON login_limits(window_start);
                    CREATE TABLE projects (
                        project_id TEXT PRIMARY KEY,
                        workspace_key TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        device_id INTEGER NOT NULL,
                        inode INTEGER NOT NULL,
                        owner_uid INTEGER NOT NULL,
                        registered_at INTEGER NOT NULL
                    );
                    CREATE TABLE workspace_lease_generations (
                        workspace_key TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        owner_id TEXT NOT NULL,
                        workflow_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        label TEXT NOT NULL,
                        token_hash TEXT NOT NULL,
                        acquired_at INTEGER NOT NULL,
                        released_at INTEGER,
                        status TEXT NOT NULL,
                        PRIMARY KEY (workspace_key, generation)
                    );
                    CREATE INDEX lease_latest ON workspace_lease_generations(
                        workspace_key, generation DESC
                    );
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                )
            elif version == 1:
                connection.execute("BEGIN EXCLUSIVE")
                try:
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(workspace_lease_generations)",
                        ).fetchall()
                    }
                    if "label" not in columns:
                        connection.execute(
                            "ALTER TABLE workspace_lease_generations "
                            "ADD COLUMN label TEXT NOT NULL DEFAULT ''",
                        )
                    connection.execute("PRAGMA user_version = 2")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        checked_path(self.directory)
        checked_path(self.path)
        connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _private_directory(path: Path) -> None:
        checked_path(path)
        if path.exists():
            info = path.stat()
            if (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Web metadata directory must be private and owned by this user")
        else:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                MetadataDatabase._private_directory(path)
