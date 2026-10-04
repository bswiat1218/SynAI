from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def process_info(pid: int) -> tuple[int, int]:
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rindex(")") + 2:].split()
    return int(fields[3]), int(fields[19])


def members(directory: Path) -> list[tuple[int, int]]:
    result = []
    for name in ("nvim.pid", "shell.pid"):
        record = directory / name
        if not record.exists():
            continue
        saved = json.loads(record.read_text())
        leader, session, start = saved["pid"], saved["session"], saved["start"]
        try:
            current = process_info(leader)
        except FileNotFoundError:
            current = None
        if current is not None and current != (session, start):
            raise RuntimeError("Editor process identity changed; refusing cleanup")
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                sid, stamp = process_info(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if sid == session:
                result.append((int(entry.name), stamp))
    return result


def terminate(directory: Path) -> None:
    processes = members(directory)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, stamp in processes:
            try:
                if process_info(pid)[1] == stamp:
                    os.kill(pid, sig)
            except (FileNotFoundError, ProcessLookupError):
                continue
        if sig == signal.SIGTERM:
            time.sleep(0.3)


def main() -> None:
    action, raw = sys.argv[1:3]
    directory = Path(raw)
    if not directory.is_absolute() or not directory.name.startswith("synai-editor-"):
        raise ValueError("Invalid editor temporary directory")
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError("Editor temporary directory ownership changed")
    if action == "spawn":
        role = sys.argv[3]
        if role not in {"nvim", "shell"}:
            raise ValueError("Invalid editor process role")
        pid = os.getpid()
        sid, stamp = process_info(pid)
        (directory / f"{role}.pid").write_text(json.dumps(
            {"pid": pid, "session": sid, "start": stamp}))
        os.execvp(sys.argv[4], sys.argv[4:])
    elif action == "jobs":
        leaders = {
            json.loads(p.read_text())["pid"]
            for p in (directory / "nvim.pid", directory / "shell.pid") if p.exists()
        }
        print(json.dumps([pid for pid, _ in members(directory) if pid not in leaders]))
    elif action in {"stop", "cleanup"}:
        terminate(directory)
        if action == "cleanup":
            import shutil
            shutil.rmtree(directory)
    else:
        raise ValueError("Invalid editor supervisor action")


if __name__ == "__main__":
    main()
