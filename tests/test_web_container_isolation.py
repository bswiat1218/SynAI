from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


class CoreContainerIsolationFixtureTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("SYNAI_RUN_CONTAINER_TESTS") == "1",
        "Set SYNAI_RUN_CONTAINER_TESTS=1 to run the disposable container fixture",
    )
    def test_private_storage_only_and_restricted_runtime(self) -> None:
        runtime = shutil.which("docker")
        if runtime is None:
            self.skipTest("Docker CLI is unavailable")
        daemon = subprocess.run(
            [runtime, "info"], capture_output=True, text=True, timeout=20,
        )
        if daemon.returncode:
            self.skipTest("Docker daemon is unavailable")
        base = subprocess.run(
            [runtime, "image", "inspect", "python:3.14-slim"],
            capture_output=True, text=True, timeout=20,
        )
        if base.returncode:
            self.skipTest("python:3.14-slim is not cached; the fixture does not pull images")
        if os.getuid() == 0:
            self.skipTest("The fixture must run with a non-root service UID")

        fixture = Path(__file__).parent / "fixtures" / "phase13c-core-isolation"
        tag = f"synai-phase13c-isolation:{uuid.uuid4().hex[:12]}"
        container_name = f"synai-phase13c-{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory(prefix="synai-phase13c-container-") as temporary:
            root = Path(temporary)
            data = root / "data"
            snapshots = root / "snapshots"
            workspace = root / "unmounted-project"
            data.mkdir(mode=0o700)
            snapshots.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            built = False
            created = False
            try:
                build = subprocess.run(
                    [runtime, "build", "--tag", tag, str(fixture)],
                    capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(build.returncode, 0, build.stderr[-2000:])
                built = True
                command = [
                    runtime, "run", "--name", container_name,
                    "--network", "none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=32",
                    "--memory=128m",
                    "--cpus=1",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                    "--mount", f"type=bind,source={data},target=/data",
                    "--mount", f"type=bind,source={snapshots},target=/snapshots",
                    tag, str(workspace),
                ]
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=60,
                )
                created = True
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                probe = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(probe["uid"], os.getuid())
                self.assertTrue(probe["writable_data_volume"])
                self.assertTrue(probe["writable_snapshot_volume"])
                self.assertTrue(probe["read_only_root"])
                self.assertTrue(probe["docker_socket_absent"])
                self.assertTrue(probe["podman_socket_absent"])
                self.assertTrue(probe["host_workspace_unmounted"])

                inspected = subprocess.run(
                    [runtime, "inspect", container_name],
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(inspected.returncode, 0, inspected.stderr[-2000:])
                container = json.loads(inspected.stdout)[0]
                host = container["HostConfig"]
                self.assertTrue(host["ReadonlyRootfs"])
                self.assertFalse(host["Privileged"])
                self.assertEqual(host["CapDrop"], ["ALL"])
                self.assertIn("no-new-privileges", host["SecurityOpt"])
                self.assertEqual(host["NetworkMode"], "none")
                self.assertEqual(host["PidsLimit"], 32)
                self.assertLessEqual(host["Memory"], 128 * 1024 * 1024)
                mounts = {entry["Destination"]: entry for entry in container["Mounts"]}
                automatic_mounts = {"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}
                self.assertEqual(
                    set(mounts) - automatic_mounts,
                    {"/data", "/snapshots", "/tmp"},
                )
                self.assertTrue(mounts["/data"]["RW"])
                self.assertTrue(mounts["/snapshots"]["RW"])
            finally:
                if created:
                    subprocess.run(
                        [runtime, "rm", "--force", container_name],
                        capture_output=True, text=True, timeout=20,
                    )
                if built:
                    subprocess.run(
                        [runtime, "image", "rm", tag],
                        capture_output=True, text=True, timeout=30,
                    )


if __name__ == "__main__":
    unittest.main()
