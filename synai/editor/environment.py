from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from synai.editor.bundle import verify_assets
from synai.editor.protocol import EditorError

ASSETS = ("init.lua", "theme.lua", "supervisor.py", "bundle.py")


@dataclass(frozen=True)
class EditorContext:
    session_id: str
    workspace: str
    mode: str
    runtime: str = ""
    container: str = ""
    uid: int = 0
    mini_path: str = ""
    shell: str = "/bin/sh"

    def validate(self) -> None:
        if self.mode not in {"host", "sandbox"} or not Path(self.workspace).is_absolute():
            raise EditorError("Invalid editor environment")
        if not Path(self.workspace).is_dir() or type(self.uid) is not int or self.uid <= 0:
            raise EditorError("Editor requires an existing workspace and non-root user")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", self.session_id):
            raise EditorError("Invalid editor conversation identity")
        if ((self.mini_path and not Path(self.mini_path).is_absolute())
                or not Path(self.shell).is_absolute()):
            raise EditorError("Plugin and shell paths must be absolute")
        if self.mode == "sandbox" and (
            self.runtime not in {"docker", "podman"}
            or self.container.startswith("-")
            or not re.fullmatch(r"[a-zA-Z0-9_.-]+", self.container)
        ):
            raise EditorError("Editor requires a validated sandbox identity")

    def payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> EditorContext:
        if not isinstance(value, dict):
            raise EditorError("Invalid editor context")
        try:
            context = cls(**value)
            context.validate()
            return context
        except (TypeError, ValueError, OSError) as exc:
            raise EditorError(f"Invalid editor context: {exc}") from exc

    @property
    def cwd(self) -> str:
        return "/workspace" if self.mode == "sandbox" else self.workspace

    def command(self, *args: str, interactive: bool = False) -> list[str]:
        if self.mode == "host":
            return list(args)
        return [
            self.runtime, "exec", "-it" if interactive else "-i",
            "--user", str(self.uid), "--workdir", "/workspace",
            self.container, *args,
        ]

    def file_path(self, value: str) -> str:
        root = Path(self.workspace).resolve(strict=True)
        target = Path(value).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise EditorError("Select a file inside the conversation workspace")
        return str(Path(self.cwd) / target.relative_to(root))


def run(context: EditorContext, *args: str, input_data: bytes | None = None,
        timeout: float = 10) -> str:
    if args and (args[0] == "nvim" or args[0].endswith("/bin/nvim")):
        # Remote clients must not fall back to writing .nvimlog in /workspace.
        args = ("env", "NVIM_LOG_FILE=/dev/null", *args)
    try:
        result = subprocess.run(
            context.command(*args), input=input_data, cwd="/",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EditorError(f"Editor environment command failed: {exc}") from exc
    if result.returncode:
        raise EditorError(result.stderr.decode(errors="replace").strip()[:4096]
                          or f"Editor environment command exited {result.returncode}")
    if len(result.stdout) > 1024 * 1024:
        raise EditorError("Editor environment response exceeds limit")
    return result.stdout.decode("utf-8")


def desktop_python() -> str:
    if sys.platform != "linux" or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise EditorError("Workspace editor needs a local Linux graphical desktop (DISPLAY or WAYLAND_DISPLAY)")
    probe = ("import sys; assert sys.version_info >= (3,9), 'Desktop Python 3.9+ required'; "
             "import gi; gi.require_version('Gtk','3.0'); gi.require_version('Vte','2.91'); "
             "from gi.repository import Gtk,Vte; assert Gtk.init_check()[0], 'Cannot connect to desktop'")
    errors = []
    for executable in dict.fromkeys((sys.executable, "/usr/bin/python3")):
        try:
            result = subprocess.run([executable, "-c", probe], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
            continue
        if result.returncode == 0:
            return executable
        errors.append(result.stderr.decode(errors="replace").strip()[-1500:])
    raise EditorError("Install Python GTK3/VTE support (python3-gi, gir1.2-gtk-3.0, "
                      "gir1.2-vte-2.91). " + "; ".join(errors))


def prepare(context: EditorContext) -> str:
    context.validate()
    vendor = Path(__file__).with_name("vendor")
    try:
        metadata = verify_assets(vendor)
        assets = {name: Path(__file__).with_name(name).read_text(encoding="utf-8") for name in ASSETS}
        assets["runtime.json"] = json.dumps(metadata)
        runtime_data = b"".join((vendor / asset["name"]).read_bytes() for asset in metadata["assets"])
    except (ValueError, OSError) as exc:
        raise EditorError(f"Bundled editor is incomplete or corrupt; reinstall SynAI: {exc}") from exc
    if context.mode == "host":
        directory = tempfile.mkdtemp(prefix="synai-editor-")
    else:
        directory = f"/tmp/synai-editor-{uuid4().hex}"
    staging = (
        "import json,os,pathlib,shutil,sys\n"
        "data=json.load(sys.stdin);p=pathlib.Path(sys.argv[1])\n"
        "assert os.geteuid()!=0, 'Editor cannot run as root'\n"
        "p.mkdir(mode=0o700,exist_ok=" + ("True" if context.mode == "host" else "False") + ")\n"
        "try:\n"
        " os.chmod(p,0o700)\n"
        " for k,v in data.items(): (p/k).write_text(v,encoding='utf-8')\n"
        " (p/'swap').mkdir(mode=0o700)\n"
        "except OSError:\n"
        " shutil.rmtree(p)\n"
        " raise\n"
    )
    staged = False
    try:
        run(context, "python3", "-c", staging, directory,
            input_data=json.dumps(assets).encode())
        staged = True
        run(context, "python3", str(Path(directory) / "bundle.py"), directory,
            input_data=runtime_data,
            timeout=30)
        check = (
            "import os,pathlib,sys;"
            "assert pathlib.Path(sys.argv[1],'lua/mini/ai.lua').is_file(), "
            "'Configured mini.nvim override is missing; unset SYNAI_MINI_PATH to use the bundled plugin';"
            "assert os.path.isfile(sys.argv[2]) and os.access(sys.argv[2],os.X_OK), "
            "'Configured shell is not executable';"
            "assert os.geteuid()!=0, 'Editor cannot run as root'"
        )
        run(context, "python3", "-c", check,
            context.mini_path or str(Path(directory) / "mini.nvim"), context.shell)
    except (EditorError, OSError):
        if context.mode == "host" and Path(directory).exists():
            shutil.rmtree(directory)
        elif staged:
            run(context, "python3", "-c",
                "import pathlib,shutil,sys;p=pathlib.Path(sys.argv[1]);"
                "shutil.rmtree(p) if p.exists() else None", directory)
        raise
    return directory


def cleanup(context: EditorContext, directory: str) -> None:
    run(context, "python3", str(Path(directory) / "supervisor.py"), "cleanup", directory, timeout=30)
