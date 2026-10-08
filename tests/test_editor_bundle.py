from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from synai.editor import bundle
from synai.editor.environment import EditorContext, cleanup, prepare
from synai.editor.neovim import Neovim
from synai.editor.protocol import EditorError


class EditorBundleTests(unittest.TestCase):
    def test_packaged_assets_have_pinned_integrity_and_notices(self) -> None:
        vendor = Path(bundle.__file__).with_name("vendor")
        data = bundle.verify_assets(vendor)
        self.assertEqual(data["platform"], bundle.PLATFORM)
        self.assertIn("Apache", (vendor / "neovim-LICENSE.txt").read_text())
        with tarfile.open(vendor / "mini.nvim.tar.gz") as archive:
            license = archive.extractfile(data["assets"][1]["root"] + "/LICENSE").read()
            self.assertIn(b"MIT", license)

    def test_corrupt_or_missing_assets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "Missing bundled"):
                bundle.verify_assets(root)
            first = bundle.manifest()["assets"][0]
            (root / first["name"]).write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "integrity"):
                bundle.verify_assets(root)

    def test_platform_checks_selected_environment(self) -> None:
        for machine, libc in (("aarch64", ("glibc", "2.36")),
                              ("x86_64", ("musl", "1.2")),
                              ("x86_64", ("glibc", "2.31"))):
            with self.subTest(machine=machine, libc=libc), \
                    patch.object(bundle.platform, "machine", return_value=machine), \
                    patch.object(bundle.platform, "libc_ver", return_value=libc):
                with self.assertRaisesRegex(ValueError, "Linux x86_64 with glibc 2.34"):
                    bundle.check_platform()

    def archive(self, root: Path, name: str, kind: bytes = tarfile.REGTYPE) -> Path:
        path = root / "asset.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.mode = 0o777
            if kind == tarfile.REGTYPE:
                entry.size = 3
                archive.addfile(entry, io.BytesIO(b"bin"))
            else:
                entry.linkname = "/tmp/outside"
                archive.addfile(entry)
        return path

    def test_safe_extraction_restores_executable_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.archive(root, "runtime/bin/nvim")
            self.assertEqual(bundle.extract(archive, root / "editor", "runtime"), 3)
            self.assertEqual((root / "editor/bin/nvim").stat().st_mode & 0o777, 0o700)

    def test_unsafe_archive_members_are_rejected_before_extraction(self) -> None:
        for name, kind in (("runtime/../../escape", tarfile.REGTYPE),
                           ("/runtime/absolute", tarfile.REGTYPE),
                           ("wrong-root/bin/nvim", tarfile.REGTYPE),
                           ("runtime/link", tarfile.SYMTYPE),
                           ("runtime/hardlink", tarfile.LNKTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = self.archive(root, name, kind)
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    bundle.extract(archive, root / "editor", "runtime")
                self.assertFalse((root / "editor").exists())

    def test_stream_integrity_and_truncation_are_checked(self) -> None:
        data = {"assets": [{"name": "asset", "size": 3,
                            "sha256": hashlib.sha256(b"abc").hexdigest()}]}
        for content, message in ((b"ab", "Truncated"), (b"xyz", "integrity"), (b"abcd", "trailing")):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, message):
                    bundle.stage(Path(directory), data, io.BytesIO(content))

    def test_insufficient_space_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.archive(root, "runtime/bin/nvim")
            with patch.object(bundle.shutil, "disk_usage",
                              return_value=SimpleNamespace(free=0)):
                with self.assertRaisesRegex(ValueError, "temporary space"):
                    bundle.extract(archive, root / "editor", "runtime")
            self.assertFalse((root / "editor").exists())

    def test_host_prepares_without_system_nvim_or_user_plugin_directory(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            context = EditorContext("bundled", workspace, "host", uid=os.getuid())
            tools = Path(home) / "tools"
            tools.mkdir()
            python = tools / "python3"
            python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
            python.chmod(0o700)
            with patch.dict(os.environ, {"PATH": str(tools), "HOME": home,
                                         "SYNAI_MINI_PATH": "/does-not-exist"}):
                directory = prepare(context)
                try:
                    nvim = Neovim(context, directory)
                    self.assertIn(nvim.binary, nvim.spawn("nvim"))
                    self.assertIn(f"SYNAI_MINI_PATH={directory}/mini.nvim", nvim.spawn("nvim"))
                    result = subprocess.run(
                        [nvim.binary, "--headless", "-u", "NONE", "-i", "NONE",
                         "+lua assert(vim.fn.has('nvim-0.11') == 1)", "+qa"],
                        capture_output=True, env=dict(os.environ, NVIM_LOG_FILE=os.devnull))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertLess(sum(path.stat().st_size for path in Path(directory).rglob("*")
                                        if path.is_file()), 192 * 1024 * 1024)
                    self.assertEqual(list(Path(workspace).iterdir()), [])
                finally:
                    cleanup(context, directory)
            self.assertFalse(Path(directory).exists())
            self.assertEqual(list(Path(home).iterdir()), [tools])

    def test_failed_host_staging_removes_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as temporary:
            context = EditorContext("bundled", workspace, "host", uid=os.getuid(), mini_path=workspace)
            with patch("synai.editor.environment.tempfile.mkdtemp",
                       return_value=str(Path(temporary) / "synai-editor-test")):
                with self.assertRaisesRegex(EditorError, "mini.nvim override"):
                    prepare(context)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_sandbox_startup_and_rpc_always_use_staged_binary(self) -> None:
        context = EditorContext("sandbox", "/tmp", "sandbox", "docker", "container", 1000)
        nvim = Neovim(context, "/tmp/synai-editor-test")
        self.assertEqual(nvim.spawn("nvim")[:9],
                         ["docker", "exec", "-it", "--user", "1000", "--workdir",
                          "/workspace", "container", "python3"])
        self.assertIn(nvim.binary, nvim.spawn("nvim"))
        with patch("synai.editor.neovim.run", return_value=json.dumps({"modified": []})) as run:
            self.assertEqual(nvim.call("state"), {"modified": []})
            self.assertEqual(run.call_args.args[:2], (context, nvim.binary))

    def test_sandbox_preparation_streams_assets_without_image_changes(self) -> None:
        context = EditorContext("sandbox", "/tmp", "sandbox", "docker", "container", 1000)
        with patch("synai.editor.environment.run", return_value="") as run:
            directory = prepare(context)
        self.assertTrue(directory.startswith("/tmp/synai-editor-"))
        self.assertEqual(len(run.call_args_list), 3)
        stage = run.call_args_list[1]
        self.assertEqual(stage.args, (context, "python3", directory + "/bundle.py", directory))
        vendor = Path(bundle.__file__).with_name("vendor")
        self.assertEqual(stage.kwargs["input_data"],
                         b"".join((vendor / asset["name"]).read_bytes()
                                  for asset in bundle.manifest()["assets"]))
        self.assertEqual(run.call_args_list[-1].args[-2:], (directory + "/mini.nvim", "/bin/sh"))

    def test_sandbox_staging_failure_is_reported_and_cleaned(self) -> None:
        context = EditorContext("sandbox", "/tmp", "sandbox", "docker", "container", 1000)
        with patch("synai.editor.environment.run",
                   side_effect=["", EditorError("incompatible glibc"), ""]) as run:
            with self.assertRaisesRegex(EditorError, "incompatible glibc"):
                prepare(context)
        self.assertEqual(len(run.call_args_list), 3)
        self.assertIn("shutil.rmtree", run.call_args_list[-1].args[3])
        self.assertEqual(run.call_args_list[-1].args[0], context)


if __name__ == "__main__":
    unittest.main()
