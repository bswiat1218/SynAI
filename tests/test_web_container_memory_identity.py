from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from synai.coding_agent.memory import (
    MemoryCategory,
    ProjectMemoryConfig,
    ProjectMemoryError,
    ProjectMemoryStore,
    project_memory_id,
)


class ContainerMemoryIdentityTests(unittest.TestCase):
    def test_disposable_bind_mount_identity_and_copied_record_retrieval(self) -> None:
        runtime = shutil.which("docker") or shutil.which("podman")
        image = os.environ.get("SYNAI_TEST_CONTAINER_IMAGE")
        if runtime is None:
            self.skipTest("Docker and Podman are unavailable; real bind-mount test was not executed")
        if not image:
            self.skipTest("Set SYNAI_TEST_CONTAINER_IMAGE to the intended SynAI service image")
        if os.getuid() == 0:
            self.skipTest("The web service must run as a non-root UID")
        service_uid = int(os.environ.get("SYNAI_TEST_SERVICE_UID", os.getuid()))
        if service_uid != os.getuid():
            self.skipTest("Copied private test data must be owned by the configured service UID")

        with tempfile.TemporaryDirectory(prefix="synai-container-identity-") as temporary:
            root = Path(temporary)
            workspace = root / "disposable-project"
            workspace.mkdir(mode=0o700)
            (workspace / "source.py").write_text("def original():\n    return 1\n")
            original_data = root / "original-data"
            store = ProjectMemoryStore(
                original_data, ProjectMemoryConfig(enabled=True),
            )
            store.initialize()
            record = store.add_memory(
                workspace,
                category=MemoryCategory.DECISION,
                title="Copied identity fixture",
                content="This historical memory is only for the disposable identity test.",
                user_authorized=True,
            )
            self.assertEqual(store.get_memory(workspace, record.memory_id), record)

            copied_data = root / "copied-data"
            copied_memory = copied_data / "project-memory"
            copied_memory.mkdir(parents=True, mode=0o700)
            copied_database = copied_memory / "memory.sqlite3"
            shutil.copy2(store.path, copied_database)
            os.chmod(copied_data, 0o700)
            os.chmod(copied_memory, 0o700)
            os.chmod(copied_database, 0o600)
            before = hashlib.sha256(copied_database.read_bytes()).hexdigest()

            inspect = subprocess.run(
                [runtime, "image", "inspect", image],
                capture_output=True,
                text=True,
            )
            if inspect.returncode:
                self.skipTest("Configured SynAI service image is not present locally")
            probe = """
import json, os, sys
from pathlib import Path
from synai.coding_agent.memory import ProjectMemoryConfig, ProjectMemoryError, ProjectMemoryStore, project_memory_id
workspace = Path('/workspace').resolve(strict=True)
info = workspace.stat()
store = ProjectMemoryStore(Path('/data'), ProjectMemoryConfig(enabled=True))
record_id = sys.argv[1]
try:
    record = store.get_memory(workspace, record_id)
    retrieved, error = True, None
except ProjectMemoryError as exc:
    retrieved, error = False, exc.code.value
print(json.dumps({
    'container_path': str(workspace),
    'effective_uid': os.getuid(),
    'device_id': info.st_dev,
    'inode': info.st_ino,
    'project_memory_id': project_memory_id(workspace),
    'retrieved_existing_record': retrieved,
    'retrieval_error': error,
}))
"""
            command = [
                runtime, "run", "--rm",
                "--user", f"{service_uid}:{os.getgid()}",
                "--network", "none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=64",
                "--memory=512m",
                "--cpus=1",
                "--volume", f"{workspace}:/workspace:ro",
                "--volume", f"{copied_data}:/data:ro",
                "--volume", f"{Path(__file__).resolve().parents[1]}:/app:ro",
                "--workdir", "/app",
                "--env", "PYTHONPATH=/app",
                image,
                "python", "-c", probe, record.memory_id,
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            container = json.loads(result.stdout.strip().splitlines()[-1])
            host_info = workspace.stat()
            host = {
                "host_path": str(workspace.resolve(strict=True)),
                "effective_uid": os.getuid(),
                "device_id": host_info.st_dev,
                "inode": host_info.st_ino,
                "project_memory_id": project_memory_id(workspace),
                "retrieved_existing_record": True,
            }
            print("MEMORY_IDENTITY " + json.dumps({"host": host, "container": container}, sort_keys=True))
            self.assertEqual(container["effective_uid"], service_uid)
            same_identity = (
                container["project_memory_id"] == host["project_memory_id"]
            )
            if same_identity:
                self.assertTrue(container["retrieved_existing_record"])
            else:
                self.assertFalse(container["retrieved_existing_record"])
                self.assertEqual(container["retrieval_error"], "memory_not_found")
            after = hashlib.sha256(copied_database.read_bytes()).hexdigest()
            self.assertEqual(before, after, "Container probe modified the copied memory database")
