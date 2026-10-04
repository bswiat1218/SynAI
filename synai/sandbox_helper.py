"""Trusted subprocess helper for container and explicitly authorized host tools."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen


ROOT = Path("/workspace")
TEMP = Path("/tmp")


class ToolFailure(Exception):
    pass


def workspace_path(value: str, *, allow_root: bool = False) -> Path:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ToolFailure("Invalid workspace path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ToolFailure("Use workspace-relative paths without '..'")
    path = ROOT / relative
    if not allow_root and path == ROOT:
        raise ToolFailure("A file path is required")
    for candidate in (path, *path.parents):
        if candidate == ROOT.parent:
            break
        if candidate.is_symlink():
            raise ToolFailure("Symlink paths are not allowed")
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ToolFailure("Path escapes workspace")
    return path


def text_file(path: Path, limit: int) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ToolFailure("File exceeds configured byte limit")
    return data.decode("utf-8")


def atomic_write(path: Path, content: str, limit: int) -> None:
    if len(content.encode("utf-8")) > limit:
        raise ToolFailure("Write exceeds configured byte limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_command(args: dict[str, Any], timeout: float, limit: int) -> dict[str, Any]:
    cwd = workspace_path(args.get("cwd", "."), allow_root=True)
    command = args["command"]
    if "${!" in command or re_unsafe(command):
        raise ToolFailure("Dynamic shell evaluation/privilege escalation is not permitted")
    process = subprocess.Popen(
        ["/bin/sh", "-c", command], cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        env={"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(TEMP), "TMPDIR": str(TEMP)},
    )
    started = time.monotonic()
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    assert process.stdout is not None and process.stderr is not None
    timed_out = truncated = False

    def kill() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map() or process.poll() is None:
                if time.monotonic() - started > timeout and not timed_out:
                    timed_out = True
                    kill()
                for key, _ in selector.select(0.05):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max(0, limit - sum(len(chunk) for chunk in chunks.values()))
                    chunks[key.data].extend(data[:remaining])
                    if len(data) > remaining:
                        truncated = True
                        kill()
            process.wait()
        finally:
            kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()
    return {
        "ok": process.returncode == 0 and not timed_out and not truncated,
        "stdout": chunks["stdout"].decode("utf-8", errors="replace"),
        "stderr": chunks["stderr"].decode("utf-8", errors="replace"),
        "exit_code": process.returncode, "duration": time.monotonic() - started,
        "timed_out": timed_out, "truncated": truncated,
    }


def re_unsafe(command: str) -> bool:
    import re

    return bool(re.search(r"\$\{[^}]*@P\}|\beval\b|\b(?:sudo|su|doas)\b", command))


def execute(request: dict[str, Any]) -> dict[str, Any]:
    if os.geteuid() == 0:
        raise ToolFailure("Root tool execution is forbidden")
    name, args = request["name"], request["arguments"]
    limit, timeout = request["limit"], request["timeout"]
    if name == "terminal":
        return run_command(args, timeout, limit)
    if name == "fetch_url":
        url = args["url"]
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ToolFailure("Only HTTP(S) URLs are allowed")
        with urlopen(url, timeout=timeout) as response:
            data = response.read(limit + 1)
            return {
                "ok": True, "status": response.status,
                "text": data[:limit].decode("utf-8", errors="replace"),
                "truncated": len(data) > limit,
            }
    path = workspace_path(args.get("path", "."), allow_root=name == "list_files")
    if name == "list_files":
        entries = []
        for entry in sorted(path.iterdir()):
            entries.append({"name": entry.name, "directory": entry.is_dir(), "symlink": entry.is_symlink()})
            if len(json.dumps(entries).encode()) > limit:
                raise ToolFailure("Directory listing exceeds output limit")
        return {"ok": True, "entries": entries}
    if name in {"read_file", "preview"}:
        content = text_file(path, limit) if path.exists() else None
        if name == "read_file" and content is None:
            raise ToolFailure("File does not exist")
        digest = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
        return {"ok": True, "content": content, "sha256": digest}
    if name in {"write_file", "patch_file", "delete_file"}:
        current = text_file(path, limit) if path.exists() else None
        digest = hashlib.sha256(current.encode()).hexdigest() if current is not None else None
        if digest != request.get("expected_sha256"):
            raise ToolFailure("File changed since approval; request the action again")
        if name == "delete_file":
            if current is None:
                raise ToolFailure("File does not exist")
            path.unlink()
        else:
            content = args["content"] if name == "write_file" else current
            if name == "patch_file":
                if current is None or not args["old"] or current.count(args["old"]) != 1:
                    raise ToolFailure("Patch old text must match exactly once")
                content = current.replace(args["old"], args["new"], 1)
            atomic_write(path, content, limit)
        return {"ok": True, "path": args["path"]}
    raise ToolFailure(f"Unknown tool: {name}")


def main() -> None:
    global ROOT, TEMP
    host = len(sys.argv) == 4 and sys.argv[1] == "--host"
    if host:
        import ctypes

        ROOT = Path(sys.argv[2]).resolve(strict=True)
        TEMP = Path(sys.argv[3]).resolve(strict=True)
        if os.geteuid() == 0:
            raise ToolFailure("Root tool execution is forbidden")
        libc = ctypes.CDLL(None, use_errno=True)
        set_no_new_privs, get_no_new_privs = 38, 39
        if libc.prctl(set_no_new_privs, 1, 0, 0, 0) != 0 or libc.prctl(get_no_new_privs, 0, 0, 0, 0) != 1:
            raise ToolFailure("Cannot enable no-new-privileges for host tools")
    request = json.loads(sys.stdin.read(4 * 1024 * 1024))
    token = request["token"]
    if not isinstance(token, str) or len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
        raise ToolFailure("Invalid execution token")
    marker = TEMP / f"coding-agent-{token}.pid"
    cancellation = TEMP / f"coding-agent-{token}.cancel"

    def cancel(signum: int, frame: Any) -> None:
        raise ToolFailure("Cancelled by user")

    signal.signal(signal.SIGTERM, cancel)
    try:
        try:
            marker.write_text(str(os.getpid()))
            if cancellation.exists():
                raise ToolFailure("Cancelled before execution")
            result = execute(request)
        except (ToolFailure, OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            result = {"ok": False, "error": str(exc)}
        print(json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        marker.unlink(missing_ok=True)
        cancellation.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
