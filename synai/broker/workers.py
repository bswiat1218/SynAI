from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from typing import TypeVar

from synai.broker.protocol import BrokerError


T = TypeVar("T")


class AuthorityWorkers:
    """Serialized thread ownership; cancellation never abandons a running thread."""

    def __init__(self, on_failure: Callable[[str], None]) -> None:
        self.on_failure = on_failure
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.closing = False
        self.unresolved = False

    async def run(self, check: Callable[[], T]) -> T:
        async with self._lock:
            if self.closing or self.unresolved:
                raise BrokerError("authority_workers_closed", "Authority workers are shutting down.", 503)
            worker = asyncio.create_task(self._execute(check))
            self._tasks.add(worker)
            try:
                while not worker.done():
                    done, _ = await asyncio.wait({worker}, timeout=1)
                    if not done:
                        self.unresolved = True
                        self.on_failure("authority_worker_verification_unresolved")
                        self.on_failure("operator_intervention_required")
                result = worker.result()
                if self.unresolved:
                    raise BrokerError("authority_worker_verification_unresolved", "Verification exceeded its assurance window.", 503)
                return result
            except asyncio.CancelledError:
                self.on_failure("authority_worker_cancelled")
                await self._join(worker)
                raise
            except Exception:
                self.on_failure("authority_worker_failed")
                raise
            finally:
                if worker.done():
                    self._tasks.discard(worker)

    @staticmethod
    async def _execute(check: Callable[[], T]) -> T:
        future = asyncio.get_running_loop().run_in_executor(
            None, contextvars.copy_context().run, check,
        )
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
        result = future.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _join(self, worker: asyncio.Task) -> bool:
        cancelled = False
        while not worker.done():
            try:
                done, _ = await asyncio.wait({worker}, timeout=1)
                if not done:
                    self.unresolved = True
                    self.on_failure("authority_worker_shutdown_unresolved")
                    self.on_failure("operator_intervention_required")
            except asyncio.CancelledError:
                cancelled = True
                self.on_failure("authority_worker_cancelled")
        try:
            worker.result()
        except asyncio.CancelledError:
            self.on_failure("authority_worker_cancelled")
        except Exception:
            self.on_failure("authority_worker_failed")
        return cancelled

    async def close(self) -> None:
        self.closing = True
        cancelled = False
        for worker in tuple(self._tasks):
            cancelled = await self._join(worker) or cancelled
            self._tasks.discard(worker)
        if cancelled:
            raise asyncio.CancelledError

    def status(self) -> dict[str, int | bool]:
        return {
            "in_flight": len(self._tasks), "closing": self.closing,
            "shutdown_unresolved": self.unresolved,
        }
