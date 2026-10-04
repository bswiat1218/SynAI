from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from synai.editor.environment import EditorContext, desktop_python
from synai.editor.protocol import MAX_MESSAGE, EditorError, decode, encode, palette


class EditorManager:
    def __init__(self, report: Callable[[str, bool], None]) -> None:
        self.report = report
        self.context: EditorContext | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.errors: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.serial = 0
        self.starting = False
        self.closing = False
        self.recovery_directory = ""

    @property
    def active(self) -> bool:
        return self.starting or (self.process is not None and self.process.returncode is None)

    def guard_context(self) -> None:
        if self.active:
            raise ValueError("Close the F7 workspace editor before changing conversation or execution environment")

    async def launch(self, context: EditorContext, colors: dict[str, Any]) -> None:
        if self.active:
            if context != self.context:
                self.guard_context()
            await self.request("focus")
            return
        context.validate()
        self.starting = True
        try:
            executable = await asyncio.to_thread(desktop_python)
            self.context = context
            self.process = await asyncio.create_subprocess_exec(
                executable, "-u", str(Path(__file__).with_name("desktop.py")),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE,
            )
            self.reader = asyncio.create_task(self.read_messages())
            self.errors = asyncio.create_task(self.read_errors())
            ready = await self.request("launch", context=context.payload(), palette=palette(colors), timeout=40)
            self.recovery_directory = str(ready.get("recovery", ""))
        except (OSError, EditorError, TimeoutError) as exc:
            await self.abort_startup()
            raise EditorError(f"Cannot open workspace editor: {exc}") from exc
        except asyncio.CancelledError:
            await asyncio.shield(self.abort_startup())
            raise
        finally:
            self.starting = False

    async def request(self, kind: str, *, timeout: float | None = 10,
                      **payload: Any) -> dict[str, Any]:
        process = self.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise EditorError("Workspace editor is not running")
        self.serial += 1
        identifier = self.serial
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            process.stdin.write(encode({"type": kind, "id": identifier, **payload}))
            await process.stdin.drain()
            result = await asyncio.wait_for(future, timeout)
            if not result.get("ok"):
                raise EditorError(str(result.get("error", "Workspace editor request failed")))
            return result
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise EditorError("Workspace editor disconnected") from exc
        finally:
            self.pending.pop(identifier, None)

    async def read_messages(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        process = self.process
        try:
            while data := await process.stdout.readline():
                value = decode(data)
                if value["type"] == "reply":
                    future = self.pending.get(value.get("id"))
                    if future is not None and not future.done():
                        future.set_result(value)
                elif value["type"] == "error":
                    self.report(str(value.get("error", "Workspace editor failure")), True)
                elif value["type"] == "closed":
                    self.report("Workspace editor closed", False)
                else:
                    raise EditorError("Unknown editor event")
        except (EditorError, ValueError, OSError) as exc:
            self.report(f"Workspace editor transport failed: {exc}", True)
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(EditorError(f"Workspace editor transport failed: {exc}"))
        finally:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                await process.wait()
            if self.process is process:
                for future in tuple(self.pending.values()):
                    if not future.done():
                        future.set_exception(EditorError("Workspace editor exited"))
            if process.returncode and not self.starting:
                self.report(f"Workspace editor exited unexpectedly ({process.returncode}); "
                            f"recovery: {self.recovery_directory or 'check desktop errors'}", True)

    async def read_errors(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        stream = self.process.stderr
        while data := await stream.read(4096):
            text = data.decode(errors="replace").strip()
            if text:
                self.report(f"Editor desktop: {text}", True)

    async def set_theme(self, colors: dict[str, Any]) -> None:
        if self.active and not self.starting and not self.closing:
            await self.request("theme", palette=palette(colors))

    async def close(self) -> bool:
        if not self.active:
            return True
        if self.starting or self.closing:
            raise EditorError("Wait for the workspace editor operation to finish")
        self.closing = True
        try:
            result = await self.request("close", timeout=None)
            if result.get("cancelled"):
                return False
            assert self.process is not None
            await asyncio.wait_for(self.process.wait(), 15)
            return True
        finally:
            self.closing = False

    async def abort_startup(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 12)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def parent_shutdown(self) -> None:
        # EOF lets the child retain dirty buffers and offer recovery rather than killing it.
        if self.process is not None and self.process.stdin is not None:
            self.process.stdin.close()
        for task in (self.reader, self.errors):
            if task is not None:
                task.cancel()
