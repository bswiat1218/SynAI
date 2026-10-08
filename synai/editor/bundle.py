"""Offline editor asset verification and staging; also runs on desktop Python 3.9+."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

PLATFORM = "manylinux_2_34_x86_64"
MAX_COMPRESSED = 32 * 1024 * 1024
MAX_EXPANDED = 192 * 1024 * 1024
RESERVE = 32 * 1024 * 1024


def manifest() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("vendor").joinpath("runtime.json").read_text())


def verify_assets(root: Path) -> dict[str, Any]:
    data = manifest()
    for asset in data["assets"]:
        path = root / asset["name"]
        if not path.is_file():
            raise ValueError(f"Missing bundled editor asset {path}; run scripts/prepare_editor_assets.py")
        if path.stat().st_size != asset["size"] or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            raise ValueError(f"Bundled editor asset integrity check failed: {asset['name']}")
    return data


def check_platform() -> None:
    libc, version = platform.libc_ver()
    if (sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}
            or libc != "glibc" or tuple(int(part) for part in version.split(".")) < (2, 34)):
        raise ValueError("Bundled editor requires Linux x86_64 with glibc 2.34+ "
                         f"(selected environment: {sys.platform}/{platform.machine()}, {libc} {version})")


def extract(archive: Path, destination: Path, root: str) -> int:
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        seen = set()
        expanded = 0
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != root
                    or not (member.isfile() or member.isdir()) or member.name in seen):
                raise ValueError(f"Unsafe bundled editor archive member: {member.name}")
            seen.add(member.name)
            expanded += member.size
            if expanded > MAX_EXPANDED or len(seen) > 20000:
                raise ValueError("Bundled editor archive exceeds staging limits")
        if shutil.disk_usage(destination.parent).free < expanded + RESERVE:
            raise ValueError("Not enough editor temporary space; reserve 32 MiB beyond the bundled runtime")
        destination.mkdir(mode=0o700)
        for member in members:
            relative = PurePosixPath(member.name).relative_to(root)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                content = source.extractfile(member)
                if content is None:
                    raise ValueError(f"Missing bundled editor archive content: {member.name}")
                with content, target.open("xb") as output:
                    shutil.copyfileobj(content, output, length=1024 * 1024)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
        return expanded


def stage(directory: Path, data: dict[str, Any], stream: BinaryIO) -> None:
    check_platform()
    total = sum(asset["size"] for asset in data["assets"])
    if total > MAX_COMPRESSED or shutil.disk_usage(directory).free < total + RESERVE:
        raise ValueError("Insufficient space or oversized bundled editor assets")
    for asset in data["assets"]:
        target = directory / asset["name"]
        remaining = asset["size"]
        digest = hashlib.sha256()
        with target.open("xb") as output:
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError("Truncated bundled editor asset stream")
                remaining -= len(chunk)
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != asset["sha256"]:
            raise ValueError(f"Bundled editor asset integrity check failed: {asset['name']}")
        if "root" in asset:
            extract(target, directory / asset["destination"], asset["root"])
            target.unlink()
    if stream.read(1):
        raise ValueError("Unexpected trailing bundled editor data")
    binary = directory / "nvim-linux-x86_64/bin/nvim"
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=10,
            env=dict(os.environ, NVIM_LOG_FILE=os.devnull))
    except OSError as exc:
        raise ValueError("Cannot execute bundled Neovim: ensure glibc 2.34+, libgcc_s.so.1 "
                         f"and executable /tmp storage: {exc}") from exc
    if result.returncode or not result.stdout.startswith(f"NVIM v{data['neovim_version']}\n"):
        raise ValueError(f"Bundled Neovim compatibility check failed: {result.stderr[:1500]}")


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    stage(directory, json.loads((directory / "runtime.json").read_text()), sys.stdin.buffer)
