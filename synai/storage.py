from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


SOURCE = Path(__file__).resolve().parent
CHECKOUT = SOURCE.parent if (SOURCE.parent / "pyproject.toml").is_file() else None


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    checked_path(path)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=True, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def checked_path(path: Path) -> Path:
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Symlink storage paths are forbidden: {part}")
    return path


def source_overlap(path: Path) -> bool:
    path = path.resolve()
    return any(
        path.is_relative_to(source) or source.is_relative_to(path)
        for source in (SOURCE, CHECKOUT) if source is not None
    )


class ConversationStorage:
    def __init__(self, root: Path) -> None:
        self.root = checked_path(root.expanduser())
        if source_overlap(self.root):
            raise ValueError("SynAI storage must not overlap its source/install directory")
        self.conversations = self.root / "conversations"

    def initialize(self) -> None:
        for path in (self.root, self.conversations):
            checked_path(path)
            if path.exists():
                info = path.stat()
                if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError(f"Storage must be a private directory owned by your user: {path}")
            else:
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    checked_path(path)
                    info = path.stat()
                    if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                        raise ValueError(f"Storage must be a private directory owned by your user: {path}")

    def folder(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid conversation ID")
        return checked_path(self.conversations / identifier)

    def record(self, identifier: str) -> Path:
        return checked_path(self.folder(identifier) / "conversation.json")

    def workspace(self, identifier: str) -> Path:
        return checked_path(self.folder(identifier) / "workspace")

    def identifier(self, record: Path) -> str:
        checked_path(record)
        identifier = record.parent.name
        if record.absolute() != self.record(identifier):
            raise ValueError("Record is outside the managed conversation layout")
        return identifier

    def create(self, identifier: str, *, workspace: bool) -> None:
        self.initialize()
        folder = self.folder(identifier)
        if folder.exists():
            info = folder.stat()
            if not folder.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError(f"Conversation folder must be private and owned by your user: {folder}")
        else:
            folder.mkdir(mode=0o700)
        if workspace:
            self.workspace(identifier).mkdir(mode=0o700, exist_ok=True)

    def validate_workspace(self, workspace: Path, identifier: str | None = None) -> Path:
        self.initialize()
        checked_path(workspace)
        if identifier is None:
            identifier = workspace.parent.name
        expected = self.workspace(identifier)
        if workspace.absolute() != expected or not expected.is_dir():
            raise ValueError("Sandbox must use its exact managed conversation workspace")
        if source_overlap(expected):
            raise ValueError("Sandbox workspace exposes SynAI source")
        info = expected.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Sandbox workspace must be private and owned by your user")
        return expected

    def discard_empty(self, identifier: str) -> None:
        workspace = self.workspace(identifier)
        if workspace.exists() and not any(workspace.iterdir()):
            workspace.rmdir()
        folder = self.folder(identifier)
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()

    def delete(self, identifier: str) -> None:
        self.initialize()
        folder = self.folder(identifier)
        if not folder.is_dir() or folder.stat().st_uid != os.getuid():
            raise ValueError("Conversation folder is missing or not owned by your user")
        mountinfo = Path("/proc/self/mountinfo")
        if mountinfo.exists():
            for line in mountinfo.read_text().splitlines():
                target = re.sub(
                    r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4],
                )
                if Path(target).is_relative_to(folder):
                    raise ValueError(f"Refusing to delete a mounted tree: {target}")
        # Never traverse mount points or follow links placed by generated programs.
        for parent, directories, _ in os.walk(folder, followlinks=False):
            for name in directories:
                child = Path(parent) / name
                if not child.is_symlink() and child.is_mount():
                    raise ValueError(f"Refusing to delete a nested mount: {child}")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("This platform cannot safely delete untrusted workspace trees")
        remaining = set(path.name for path in folder.iterdir()) - {"conversation.json", "workspace"}
        if remaining:
            raise ValueError(f"Unexpected conversation files retained: {', '.join(sorted(remaining))}")
        workspace = self.workspace(identifier)
        if workspace.exists():
            shutil.rmtree(workspace)
        record = self.record(identifier)
        backup = record.read_bytes()
        record.unlink()
        try:
            folder.rmdir()
        except OSError as exc:
            try:
                record = self.record(identifier)
                descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(backup)
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, ValueError) as restore:
                raise OSError(f"Folder removal failed: {exc}; metadata recovery failed: {restore}") from exc
            raise

    def write_receipt(self, value: dict[str, Any]) -> None:
        self.initialize()
        receipt = checked_path(self.root / "legacy-cleanup.json")
        write_private_json(receipt, value)
