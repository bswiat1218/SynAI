from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from synai.broker.protocol import BrokerError


class ChildProcesses:
    def __init__(self, on_failure: Callable[[str], None]) -> None:
        self.on_failure = on_failure
        self.spawns: set[asyncio.Task[asyncio.subprocess.Process]] = set()
        self.children: set[asyncio.subprocess.Process] = set()
        self.collections: set[asyncio.Future] = set()
        self.reaps: dict[asyncio.subprocess.Process, asyncio.Task[int]] = {}
        self.unresolved = False

    def _failed(self) -> None:
        self.unresolved = True
        self.on_failure("runtime_process_lifetime_unresolved")
        self.on_failure("operator_intervention_required")

    def _late_spawn(self, task: asyncio.Task[asyncio.subprocess.Process]) -> None:
        self.spawns.discard(task)
        if task.cancelled():
            self._failed()
            return
        try:
            process = task.result()
        except Exception:
            self._failed()
            return
        self.children.add(process)
        try:
            if process.returncode is None:
                process.kill()
        except OSError:
            self._failed()
        self._start_reap(process)

    def _start_reap(self, process: asyncio.subprocess.Process) -> asyncio.Task[int]:
        reap = self.reaps.get(process)
        if reap is None:
            reap = asyncio.create_task(process.wait())
            self.reaps[process] = reap
            reap.add_done_callback(lambda task: self._reaped(process, task))
        return reap

    async def cleanup(self, process: asyncio.subprocess.Process, deadline: float) -> bool:
        if process not in self.children:
            self._failed()
            return False
        loop = asyncio.get_running_loop()
        if process.returncode is None:
            try:
                process.kill()
            except OSError:
                self._failed()
        reap = self._start_reap(process)
        cancelled = False
        try:
            while not reap.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self._failed()
                    return False
                try:
                    await asyncio.wait({reap}, timeout=remaining)
                except asyncio.CancelledError:
                    cancelled = True
                    self.on_failure("runtime_process_cleanup_cancelled")
            try:
                reap.result()
            except (OSError, RuntimeError):
                self._failed()
                return False
            if process.returncode is None:
                self._failed()
                return False
            self.children.discard(process)
            return True
        finally:
            if cancelled:
                raise asyncio.CancelledError

    def _reaped(self, process: asyncio.subprocess.Process, task: asyncio.Task[int]) -> None:
        self.reaps.pop(process, None)
        if task.cancelled():
            self._failed()
        elif task.exception() is not None or process.returncode is None:
            self._failed()
        else:
            self.children.discard(process)

    def status(self) -> dict[str, int | bool]:
        return {
            "pending_spawns": len(self.spawns), "owned_children": len(self.children),
            "pending_reaps": len(self.reaps), "pending_output": len(self.collections),
            "lifetime_unresolved": self.unresolved,
        }

    async def run(
        self, executable: str, args: tuple[str, ...], env: dict[str, str],
        timeout: float, stdout_limit: int, stderr_limit: int,
        reader: Callable[[asyncio.StreamReader, int], Awaitable[bytes]],
        on_spawn: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> tuple[int, bytes, bytes]:
        if self.unresolved:
            raise BrokerError("runtime_process_lifetime_unresolved", "A prior child lifetime is unresolved.", 503)
        loop = asyncio.get_running_loop()
        if timeout <= 0:
            raise TimeoutError("Child lifetime deadline has already expired")
        deadline = loop.time() + timeout
        # Reserve cleanup inside the same total budget, not an additional timeout.
        work_deadline = deadline - min(0.5, timeout / 4)
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            executable, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        ))
        self.spawns.add(spawn)
        process = None
        collection = None
        try:
            async with asyncio.timeout_at(work_deadline):
                process = await asyncio.shield(spawn)
                self.spawns.discard(spawn)
                self.children.add(process)
                if on_spawn is not None:
                    on_spawn(process)
                if process.stdout is None or process.stderr is None:
                    raise BrokerError("runtime_process_pipes_unavailable", "Child pipes are unavailable.", 503)
                collection = asyncio.gather(
                    reader(process.stdout, stdout_limit),
                    reader(process.stderr, stderr_limit),
                    process.wait(),
                )
                self.collections.add(collection)
                collection.add_done_callback(self.collections.discard)
                stdout, stderr, _ = await asyncio.shield(collection)
                if process.returncode is None:
                    raise BrokerError("runtime_process_lifetime_unresolved", "Child exit was not established.", 503)
                self.children.discard(process)
                return process.returncode, stdout, stderr
        except (Exception, asyncio.CancelledError) as exc:
            if process is None:
                self._failed()
                spawn.add_done_callback(self._late_spawn)
            else:
                if collection is not None:
                    collection.cancel()
                    collection.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                if not await self.cleanup(process, deadline):
                    if not isinstance(exc, asyncio.CancelledError):
                        raise BrokerError(
                            "runtime_process_lifetime_unresolved",
                            "Exact child termination or reaping could not be established.",
                            503,
                        ) from exc
            raise

    async def close(self) -> None:
        deadline = asyncio.get_running_loop().time() + 1
        if self.spawns:
            await asyncio.wait(self.spawns, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        for process in tuple(self.children):
            await self.cleanup(process, deadline)
        if self.collections:
            await asyncio.wait(self.collections, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        if self.spawns or self.children or self.collections or self.reaps:
            self._failed()
