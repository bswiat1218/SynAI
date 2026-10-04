from __future__ import annotations

import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


SPEC = importlib.util.spec_from_file_location(
    "release_verifier", Path(__file__).resolve().parents[1] / "scripts/verify_release.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class ArtifactValidationTests(unittest.TestCase):
    def make_wheel(self, root: Path, extra: dict[str, str] | None = None,
                   missing: str | None = None) -> Path:
        wheel = root / "synai-0.3.0-py3-none-any.whl"
        prefix = "synai-0.3.0.dist-info/"
        entries = {name: "runtime resource" for name in verifier.RESOURCES}
        entries.update({
            prefix + "METADATA": (
                "Metadata-Version: 2.4\nName: synai\nVersion: 0.3.0\n"
                "Requires-Python: >=3.12\nLicense-Expression: Apache-2.0\n"
                "License-File: LICENSE\nLicense-File: NOTICE\n"
                "Description-Content-Type: text/markdown\n"
                "Classifier: Operating System :: POSIX :: Linux\n\nREADME\n"),
            prefix + "entry_points.txt": "[console_scripts]\nsynai = synai.cli:main\n",
            prefix + "licenses/LICENSE": "Apache license",
            prefix + "licenses/NOTICE": "SynAI contributors",
        })
        entries.update(extra or {})
        if missing:
            entries.pop(missing)
        with ZipFile(wheel, "w") as archive:
            for name, text in entries.items():
                archive.writestr(name, text)
        return wheel

    def test_correct_metadata_and_resources_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = self.make_wheel(Path(directory))
            version, runtime = verifier.verify_wheel(wheel)
            self.assertEqual(version, "0.3.0")
            self.assertEqual(set(runtime), verifier.RESOURCES)

    def test_missing_resource_generic_module_and_private_data_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for extra, missing, message in (
                ({}, "synai/editor/theme.lua", "missing runtime"),
                ({"config.py": "unsafe"}, None, "top-level"),
                ({"synai/__pycache__/config.pyc": "unsafe"}, None, "private data"),
            ):
                with self.subTest(message=message):
                    wheel = self.make_wheel(Path(directory), extra, missing)
                    with self.assertRaisesRegex(ValueError, message):
                        verifier.verify_wheel(wheel)

    def test_source_symlinks_and_unrelated_files_rejected_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synai-0.3.0.tar.gz"
            for link, filename, message in (
                (True, "synai/file.py", "special files"),
                (False, "settings.json", "Unexpected source"),
                (False, "results/private.json", "private data"),
            ):
                with self.subTest(filename=filename):
                    with tarfile.open(source, "w:gz") as archive:
                        member = tarfile.TarInfo("synai-0.3.0/" + filename)
                        if link:
                            member.type = tarfile.SYMTYPE
                            member.linkname = "/tmp/outside"
                            archive.addfile(member)
                        else:
                            member.size = 1
                            archive.addfile(member, io.BytesIO(b"x"))
                    with self.assertRaisesRegex(ValueError, message):
                        verifier.verify_source(source, "0.3.0", root / "unpack")
                    self.assertFalse((root / "unpack").exists())


if __name__ == "__main__":
    unittest.main()
