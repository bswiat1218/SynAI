from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synai.intelligence import RepositoryIndex
from synai.web.config import WebConfig, WorkspaceMountConfig
from synai.web.database import MetadataDatabase
from synai.web.ownership import DataRootOwnership, OwnershipConflict
from synai.web.projects import ProjectRegistry
from synai.web.workspaces import (
    InterruptedLease,
    LeaseRecoveryRequired,
    RecoveryEvidence,
    WorkspaceContention,
    WorkspaceCoordinator,
    WorkspaceOwner,
    capture_preimage,
    preimage_is_current,
)


class WorkspaceCoordinationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = MetadataDatabase(self.data_root)
        self.database.initialize()
        config = WebConfig(
            self.data_root,
            (WorkspaceMountConfig("main", self.workspace),),
            initial_password="long enough test passphrase",
        )
        self.identity = ProjectRegistry(self.database, config).register("main").identity
        self.assertIsNotNone(self.identity)
        self.coordinator = WorkspaceCoordinator(self.database, poll_seconds=0.01)
        self.owner = WorkspaceOwner("web", "task-workflow", "task-1", "Agent Task")

    async def test_lease_reentrancy_contention_and_release(self) -> None:
        first = await self.coordinator.acquire(self.identity, self.owner)
        nested = await self.coordinator.acquire(self.identity, self.owner)
        self.assertEqual(first.generation, nested.generation)
        self.assertTrue(self.coordinator.validate_authority(
            self.identity, self.owner, first.fencing_token,
        ))
        wrong_task = WorkspaceOwner(
            self.owner.owner_id, self.owner.workflow_id, "other-task", self.owner.label,
        )
        self.assertFalse(self.coordinator.validate_authority(
            self.identity, wrong_task, first.fencing_token,
        ))
        other = WorkspaceOwner("web", "different-workflow", "task-2", "repair")
        with self.assertRaises(WorkspaceContention):
            await self.coordinator.acquire(self.identity, other, timeout=0.05)
        await first.release()
        self.assertTrue(self.coordinator.validate_authority(
            self.identity, self.owner, nested.fencing_token,
        ))
        await nested.release()
        self.assertFalse(self.coordinator.validate_authority(
            self.identity, self.owner, nested.fencing_token,
        ))
        next_lease = await self.coordinator.acquire(self.identity, other)
        self.assertEqual(next_lease.generation, first.generation + 1)
        self.assertFalse(self.coordinator.validate_authority(
            self.identity, self.owner, first.fencing_token,
        ))
        self.assertTrue(self.coordinator.validate_authority(
            self.identity, other, next_lease.fencing_token,
        ))
        await next_lease.release()

    async def test_cancellation_does_not_leak_in_process_or_os_lock(self) -> None:
        lease = await self.coordinator.acquire(self.identity, self.owner)
        other = WorkspaceOwner("web", "other-flow", "task-2", "verification")
        waiting = asyncio.create_task(
            self.coordinator.acquire(self.identity, other, timeout=10),
        )
        await asyncio.sleep(0.02)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await lease.release()
        replacement = await self.coordinator.acquire(self.identity, other, timeout=1)
        await replacement.release()

    async def test_process_crash_requires_explicit_interrupted_owner_recovery(self) -> None:
        script = """
import asyncio, os, sys
from pathlib import Path
from synai.web.config import WebConfig, WorkspaceMountConfig
from synai.web.database import MetadataDatabase
from synai.web.projects import ProjectRegistry
from synai.web.workspaces import WorkspaceCoordinator, WorkspaceOwner
root, workspace = Path(sys.argv[1]), Path(sys.argv[2])
database = MetadataDatabase(root)
database.initialize()
config = WebConfig(root, (WorkspaceMountConfig('main', workspace),), initial_password='long enough test passphrase')
identity = ProjectRegistry(database, config).register('main').identity
lease = asyncio.run(WorkspaceCoordinator(database).acquire(
    identity, WorkspaceOwner('child', 'crashed-flow', 'task-x', 'test'),
))
print(lease.generation, flush=True)
os._exit(0)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.data_root), str(self.workspace)],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "1")
        with self.assertRaises(LeaseRecoveryRequired):
            await self.coordinator.acquire(
                self.identity, self.owner, recover_interrupted=True,
            )

        class Recovery:
            async def confirm_stopped(self, interrupted: InterruptedLease) -> RecoveryEvidence:
                return RecoveryEvidence(
                    interrupted.workspace_identity.key,
                    interrupted.owner.owner_id,
                    interrupted.owner.workflow_id,
                    interrupted.owner.task_id,
                    interrupted.generation,
                    True,
                    "test-reaped-process-tree",
                    1,
                )

        class UnconfirmedRecovery:
            async def confirm_stopped(self, interrupted: InterruptedLease) -> RecoveryEvidence:
                return RecoveryEvidence(
                    interrupted.workspace_identity.key,
                    interrupted.owner.owner_id,
                    interrupted.owner.workflow_id,
                    interrupted.owner.task_id,
                    interrupted.generation,
                    False,
                    "writer-still-running",
                    1,
                )

        with self.assertRaises(LeaseRecoveryRequired):
            await self.coordinator.acquire(
                self.identity,
                self.owner,
                recover_interrupted=True,
                recovery_authority=UnconfirmedRecovery(),
            )
        recovered = await self.coordinator.acquire(
            self.identity,
            self.owner,
            recover_interrupted=True,
            recovery_authority=Recovery(),
        )
        self.assertEqual(recovered.generation, 2)
        await recovered.release()

    async def test_external_file_change_invalidates_no_follow_preimage(self) -> None:
        target = self.workspace / "source.txt"
        target.write_text("before")
        repository = RepositoryIndex(self.workspace)
        expected = capture_preimage(self.identity, repository, "source.txt")
        self.assertTrue(preimage_is_current(self.identity, repository, expected))
        target.write_text("after")
        self.assertFalse(preimage_is_current(self.identity, repository, expected))
        outside = self.root / "outside.txt"
        outside.write_text("private")
        (self.workspace / "source.txt").unlink()
        (self.workspace / "source.txt").symlink_to(outside)
        with self.assertRaises(ValueError):
            capture_preimage(self.identity, repository, "source.txt")


class DataRootOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "synai-data"

    def test_tui_and_web_cannot_own_same_data_root(self) -> None:
        tui = DataRootOwnership(self.root, "tui")
        web = DataRootOwnership(self.root, "web")
        owner = tui.acquire()
        with self.assertRaisesRegex(OwnershipConflict, f"PID {os.getpid()} \\(tui\\)"):
            web.acquire()
        tui.release()
        web.acquire()
        web.release()
        self.assertEqual(owner.mode, "tui")

    def test_cross_process_lock_and_automatic_crash_release(self) -> None:
        script = """
import os, sys, time
from pathlib import Path
from synai.web.ownership import DataRootOwnership
lock = DataRootOwnership(Path(sys.argv[1]), 'web')
lock.acquire()
print('ready', flush=True)
time.sleep(30)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root)],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with self.assertRaises(OwnershipConflict):
                DataRootOwnership(self.root, "tui").acquire()
            process.kill()
            process.wait(timeout=5)
            with DataRootOwnership(self.root, "tui"):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
