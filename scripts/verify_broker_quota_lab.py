#!/usr/bin/env python3
"""Explicit opt-in project-quota exercise for a disposable Broker lab filesystem."""

from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
from pathlib import Path

from synai.broker.quota_slots import (
    _FS_XFLAG_PROJINHERIT,
    _mount_for_fd,
    _open_directory_chain,
    _project_attributes,
    _read_project_quota,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount-path", type=Path, required=True)
    parser.add_argument("--slot-path", type=Path, required=True)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--mode", choices=("bytes", "inodes"), required=True)
    parser.add_argument("--byte-limit", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--inode-limit", type=int, default=4096)
    parser.add_argument(
        "--confirm-write-test",
        action="store_true",
        help="Explicitly authorize creating persistent test files; this script never removes them.",
    )
    args = parser.parse_args()
    mount_path = args.mount_path.resolve(strict=True)
    slot_path = args.slot_path.resolve(strict=True)
    if (
        not args.confirm_write_test
        or not mount_path.is_absolute()
        or not slot_path.is_relative_to(mount_path)
        or slot_path == mount_path
        or not args.slot_path.is_absolute()
        or args.slot_path != Path(os.path.normpath(args.slot_path))
    ):
        parser.error("Use canonical paths within the dedicated mount and pass --confirm-write-test.")
    if args.project_id <= 0 or args.byte_limit <= 0 or args.inode_limit <= 0:
        parser.error("Project ID and hard quota limits must be positive.")

    slot_fd, descriptors = _open_directory_chain(slot_path)
    try:
        mount = _mount_for_fd(slot_fd)
        if (
            mount.target != str(mount_path)
            or mount.root != "/"
            or mount.filesystem != "ext4"
            or not ({"prjquota", "pquota"} & (mount.mount_options | mount.super_options))
        ):
            parser.error("Selected path is not on the expected ext4 project-quota mount.")
        project_id, flags = _project_attributes(slot_fd)
        if project_id != args.project_id or not flags & _FS_XFLAG_PROJINHERIT:
            parser.error("Slot directory project ID or inheritance does not match.")
        quota = _read_project_quota(mount_path, args.project_id)
        if (
            quota.byte_limit != args.byte_limit
            or quota.inode_limit != args.inode_limit
            or quota.bytes_used > quota.byte_limit
            or quota.inodes_used > quota.inode_limit
        ):
            parser.error("Live hard quotas do not match the requested test profile.")
        statvfs = os.statvfs(mount_path)
        if statvfs.f_bavail * statvfs.f_frsize < args.byte_limit + 1024 * 1024 * 1024:
            parser.error("Insufficient filesystem headroom for a safe isolated quota test.")
        if os.listdir(slot_fd):
            parser.error("Selected slot must be empty; existing lab evidence is never removed.")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    evidence_dir = slot_path / f"quota-lab-{args.mode}-{secrets.token_hex(8)}"
    evidence_dir.mkdir(mode=0o700)
    try:
        evidence_fd = os.open(
            evidence_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            inherited_id, inherited_flags = _project_attributes(evidence_fd)
            if inherited_id != args.project_id or not inherited_flags & _FS_XFLAG_PROJINHERIT:
                raise RuntimeError("New evidence directory did not inherit the configured quota.")
            if args.mode == "bytes":
                _fill_bytes(evidence_dir)
            else:
                _fill_inodes(evidence_dir)
        finally:
            os.close(evidence_fd)
    except OSError as exc:
        if exc.errno not in {errno.ENOSPC, getattr(errno, "EDQUOT", 122)}:
            raise
        enforcement_result = f"quota_limit_reached_errno_{exc.errno}"
    else:
        enforcement_result = "limit_not_reached"

    after = _read_project_quota(mount_path, args.project_id)
    print(json.dumps({
        "schema_version": 1,
        "mode": args.mode,
        "project_id": args.project_id,
        "result": enforcement_result,
        "evidence_directory": str(evidence_dir),
        "quota_before": {
            "bytes_used": quota.bytes_used,
            "inodes_used": quota.inodes_used,
            "byte_limit": quota.byte_limit,
            "inode_limit": quota.inode_limit,
        },
        "quota_after": {
            "bytes_used": after.bytes_used,
            "inodes_used": after.inodes_used,
            "byte_limit": after.byte_limit,
            "inode_limit": after.inode_limit,
        },
        "cleanup": "not_performed; preserve lab evidence",
        "production_qualified": False,
    }, sort_keys=True))
    return 0 if enforcement_result.startswith("quota_limit_reached") else 1


def _fill_bytes(directory: Path) -> None:
    path = directory / "byte-limit-evidence.bin"
    chunk = b"\0" * (1024 * 1024)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        while True:
            written = os.write(descriptor, chunk)
            if written != len(chunk):
                raise OSError(errno.ENOSPC, "Short write during project quota test")
    finally:
        os.close(descriptor)


def _fill_inodes(directory: Path) -> None:
    index = 0
    while True:
        descriptor = os.open(
            directory / f"inode-{index:06d}",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        os.close(descriptor)
        index += 1


if __name__ == "__main__":
    raise SystemExit(main())
