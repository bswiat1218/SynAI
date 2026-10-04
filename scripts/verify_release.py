"""Check archives, clean installs, and a wheel rebuilt from the source archive."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

RESOURCES = {
    "synai/tui/theme.tcss", "synai/editor/init.lua", "synai/editor/theme.lua",
    "synai/editor/supervisor.py", "synai/editor/desktop.py", "synai/sandbox_helper.py",
    "synai/editor/sandbox-editor.Dockerfile", "synai/__main__.py",
}
FORBIDDEN = {".venv", ".git", "__pycache__", "results", "prompts", ".synai", "build", "dist"}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def run(*args: str, cwd: Path, env: dict[str, str]) -> str:
    print("+", " ".join(args), flush=True)
    result = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=1800)
    if result.returncode:
        raise RuntimeError(result.stdout)
    print(result.stdout.rstrip(), flush=True)
    return result.stdout


def verify_wheel(path: Path) -> tuple[str, dict[str, bytes]]:
    with ZipFile(path) as archive:
        names = archive.namelist()
        check(len(names) == len(set(names)), "Duplicate wheel entries")
        check(all(not (set(PurePosixPath(name).parts) & FORBIDDEN) for name in names),
              "Wheel contains private data or build/cache files")
        check(all(name.startswith("synai/") or re.fullmatch(r"synai-[^/]+\.dist-info/.*", name)
                  for name in names), "Wheel contains non-SynAI top-level modules")
        check(RESOURCES <= set(names), "Wheel is missing runtime resources")
        info = [name for name in names if name.endswith(".dist-info/METADATA")]
        check(len(info) == 1, "Wheel needs exactly one distribution metadata file")
        data = BytesParser().parsebytes(archive.read(info[0]))
        version = data["Version"]
        check(data["Name"] == "synai", "Unexpected distribution name")
        check(bool(re.fullmatch(r"\d+\.\d+\.\d+", version or "")), "Invalid release version")
        check(data["Requires-Python"] == ">=3.12", "Unexpected Python requirement")
        check(data["License-Expression"] == "Apache-2.0", "Missing SPDX license")
        check(set(data.get_all("License-File", [])) == {"LICENSE", "NOTICE"}, "Missing license files")
        check("text/markdown" in data["Description-Content-Type"], "Missing rendered README metadata")
        check("Operating System :: POSIX :: Linux" in data.get_all("Classifier", []), "Missing Linux support metadata")
        prefix = info[0].removesuffix("METADATA")
        for license in ("LICENSE", "NOTICE"):
            check(prefix + "licenses/" + license in names, f"Missing bundled {license}")
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(prefix + "entry_points.txt").decode())
        check(dict(parser["console_scripts"]) == {"synai": "synai.cli:main"}, "Incorrect console entry point")
        runtime = {name: archive.read(name) for name in names if name.startswith("synai/")}
        return version, runtime


def verify_source(path: Path, version: str, destination: Path) -> Path:
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        prefix = f"synai-{version}"
        check(len(names) == len(set(names)), "Duplicate source entries")
        check(all(PurePosixPath(name).parts[0] == prefix for name in names), "Unexpected source archive root")
        for member in members:
            parts = PurePosixPath(member.name).parts
            check(not (set(parts[1:]) & FORBIDDEN), "Source archive contains private data or caches")
            check(".." not in parts and not member.name.startswith("/"), "Unsafe archive path")
            check(member.isfile() or member.isdir(), "Source archive contains special files or links")
            if len(parts) > 1:
                check(parts[1] in {
                    "synai", "synai.egg-info", "tests", "scripts", "docs", ".github",
                    "README.md", "LICENSE", "NOTICE", "pyproject.toml", "MANIFEST.in",
                    "app.py", "PKG-INFO", "setup.cfg",
                }, f"Unexpected source archive entry: {member.name}")
        required = RESOURCES | {
            "README.md", "LICENSE", "NOTICE", "pyproject.toml", "MANIFEST.in", "app.py",
            "tests/support.py", "tests/test_cli.py", "scripts/verify_release.py",
            "scripts/release_smoke.py", "docs/RELEASING.md",
            ".github/workflows/ci.yml", ".github/workflows/publish.yml",
        }
        check({prefix + "/" + name for name in required} <= set(names), "Source archive is incomplete")
        data = BytesParser().parsebytes(archive.extractfile(prefix + "/PKG-INFO").read())
        check(data["Version"] == version, "Source/wheel versions differ")
        archive.extractall(destination, filter="data")
    return destination / prefix


def smoke(wheel: Path, root: Path, source: Path, env: dict[str, str], full_tests: bool) -> None:
    root.mkdir()
    environment = root / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = str(environment / "bin/python")
    command = str(environment / "bin/synai")
    working = root / "working"
    working.mkdir()
    home = root / "home"
    home.mkdir()
    isolated = {**env, "HOME": str(home)}
    run(python, "-m", "pip", "install", str(wheel), cwd=working, env=isolated)
    for name in ("config", "models", "tools", "app", "tui", "providers", "synai_editor"):
        (working / (name + ".py")).write_text("raise RuntimeError('Imported generic decoy module')\n")
    version = verify_wheel(wheel)[0]
    for args in (("--help",), ("--version",), ("--print-editor-image-recipe",)):
        output = run(command, *args, cwd=working, env=isolated)
        if args == ("--version",):
            check(output.strip() == f"SynAI {version}", "Console version mismatch")
        if args == ("--print-editor-image-recipe",):
            check(output.encode() == verify_wheel(wheel)[1]["synai/editor/sandbox-editor.Dockerfile"],
                  "Printed image recipe differs from packaged resource")
    check(not (home / ".synai").exists(), "Early-exit CLI wrote user storage")
    run(python, "-m", "synai", "--version", cwd=working, env=isolated)
    run(python, "-c",
        "import sys; import synai.cli; "
        "assert 'synai.tui.application' not in sys.modules; "
        "assert 'gi' not in sys.modules; "
        "from synai.tui.application import CodingApp; "
        "assert not any(n in sys.modules for n in ('config','models','tools','app','tui','providers','synai_editor'))",
        cwd=working, env=isolated)
    run(python, "-I", str(source / "scripts/release_smoke.py"), cwd=working, env=isolated)
    if full_tests:
        run(python, "-m", "unittest", "discover", "-s", str(source / "tests"), "-q",
            cwd=working, env=isolated)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--tag", help="Require an exact vX.Y.Z tag matching artifact metadata")
    parser.add_argument("--run-tests", action="store_true", help="Run full normal suite against the source-rebuilt installation")
    args = parser.parse_args()
    distribution = args.dist_dir.resolve(strict=True)
    wheels = list(distribution.glob("*.whl"))
    sources = list(distribution.glob("*.tar.gz"))
    check(len(wheels) == len(sources) == 1, "Use a clean dist directory containing exactly one wheel/source pair")
    check({p.name for p in distribution.iterdir()} <= {wheels[0].name, sources[0].name, "SHA256SUMS"},
          "Distribution directory contains unexpected files")
    version, runtime = verify_wheel(wheels[0])
    if args.tag is not None:
        check(args.tag == f"v{version}", "Tag must exactly match the release version")
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "AGENT_CONTAINER_TESTS", "SYNAI_TEST_DESKTOP", "SYNAI_TEST_WINDOW_MANAGER",
                "SYNAI_TEST_MINI_PATH", "SYNAI_TEST_EDITOR_IMAGE"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="synai-release-") as directory:
        temporary = Path(directory)
        source = verify_source(sources[0], version, temporary / "source")
        rebuilt = temporary / "rebuilt"
        run(sys.executable, "-m", "build", "--wheel", "--outdir", str(rebuilt),
            str(source), cwd=temporary, env=env)
        rebuilt_wheels = list(rebuilt.glob("*.whl"))
        check(len(rebuilt_wheels) == 1, "Source rebuild did not produce exactly one wheel")
        rebuilt_version, rebuilt_runtime = verify_wheel(rebuilt_wheels[0])
        check(rebuilt_version == version and runtime == rebuilt_runtime,
              "Source-rebuilt wheel has different runtime content")
        smoke(wheels[0], temporary / "direct", source, env, False)
        smoke(rebuilt_wheels[0], temporary / "from-source", source, env, args.run_tests)
    checksum = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                       for path in sorted((wheels[0], sources[0])))
    (distribution / "SHA256SUMS").write_text(checksum)
    print(f"Verified SynAI {version}: direct wheel and source-rebuilt installation. Checksums written.")


if __name__ == "__main__":
    main()
