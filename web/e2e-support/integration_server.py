from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import uvicorn

from synai.models import ChatEvent, Message, ModelInfo
from synai.providers.errors import ProviderError
from synai.web.app import create_app
from synai.web.config import WebConfig


class BrowserAcceptanceProvider:
    def __init__(self, state_path: Path, call_log: Path, tool_sentinel: Path) -> None:
        self.state_path = state_path
        self.call_log = call_log
        self.tool_sentinel = tool_sentinel
        self.tool_sentinel.parent.mkdir(parents=True, exist_ok=True)

    def _state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return value if isinstance(value, dict) else {}

    async def list_models(self) -> list[ModelInfo]:
        if self._state().get("unavailable") is True:
            raise ProviderError("Deterministic test provider is unavailable.")
        return [
            ModelInfo("fake-fast", tools=True, thinking=True),
            ModelInfo("fake-second", tools=False, thinking=False),
            ModelInfo("fake-slow", tools=True, thinking=False),
            ModelInfo("fake-tool", tools=True, thinking=False),
        ]

    async def capabilities(self, name: str) -> ModelInfo:
        models = await self.list_models()
        for model in models:
            if model.name == name:
                return model
        raise ProviderError("Model is unavailable.")

    async def chat(
        self, model: str, messages: list[Message], tools: list[dict[str, Any]],
    ):
        with self.call_log.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"model": model, "tools": len(tools)}) + "\n")
            log.flush()
            os.fsync(log.fileno())

        if model == "fake-tool":
            yield ChatEvent(
                tool_calls=[{
                    "function": {
                        "name": "terminal",
                        "arguments": {"command": f"touch {self.tool_sentinel}"},
                    },
                }],
                done=True,
            )
            return

        prompt = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        if model == "fake-slow":
            yield ChatEvent(content="Partial output before cancellation.")
            await asyncio.Event().wait()
            return
        if model == "fake-fast" and prompt == "long code":
            answer = "```text\n" + ("x" * 512) + "\n```"
        elif model == "fake-fast" and prompt == "html injection":
            answer = '<img src=x onerror="window.__synaiInjected = true">'
        else:
            answer = f"Integration response: {prompt}"
        if model == "fake-fast":
            yield ChatEvent(thinking="Private deterministic reasoning.")
        for start in range(0, len(answer), 12):
            yield ChatEvent(content=answer[start:start + 12])
            await asyncio.sleep(0.025)
        yield ChatEvent(done=True)

    async def close(self) -> None:
        return None


def _run_api(config: WebConfig, state_path: Path, call_log: Path, tool_sentinel: Path) -> None:
    app = create_app(
        config,
        provider=BrowserAcceptanceProvider(state_path, call_log, tool_sentinel),
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        access_log=False,
        log_level="warning",
        proxy_headers=False,
    )


def _wait_for_api() -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/api/v1/health", timeout=0.5):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("Isolated Phase 13D API did not start.")


def main() -> None:
    state_path = Path(os.environ["SYNAI_E2E_PROVIDER_STATE"])
    call_log = Path(os.environ["SYNAI_E2E_PROVIDER_CALLS"])
    control_path = Path(os.environ["SYNAI_E2E_CONTROL"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    call_log.parent.mkdir(parents=True, exist_ok=True)
    control_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="synai-phase13d-") as directory:
        data_root = Path(directory) / "data"
        tool_sentinel = Path(os.environ["SYNAI_E2E_TOOL_SENTINEL"])
        config = WebConfig(
            data_root=data_root,
            workspace_mounts=(),
            initial_password=os.environ["SYNAI_E2E_PASSWORD"],
            ollama_url="http://127.0.0.1:11434",
            public_origin="http://127.0.0.1:4179",
            bind_host="127.0.0.1",
            port=8765,
            session_lifetime_seconds=300,
        )
        state_path.write_text(json.dumps({"unavailable": False}), encoding="utf-8")
        call_log.write_text("", encoding="utf-8")
        control_path.write_text(json.dumps({"restart": False}), encoding="utf-8")
        restart_count = 0
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        def start_server() -> multiprocessing.Process:
            process = multiprocessing.Process(
                target=_run_api,
                args=(config, state_path, call_log, tool_sentinel),
                daemon=True,
            )
            process.start()
            return process

        server = start_server()
        _wait_for_api()
        restart_count += 1
        control_path.write_text(
            json.dumps({"restart": False, "restart_count": restart_count}),
            encoding="utf-8",
        )
        try:
            while not stopping:
                try:
                    control = json.loads(control_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.1)
                    continue
                if isinstance(control, dict) and control.get("restart") is True:
                    server.terminate()
                    server.join(timeout=5)
                    if server.is_alive():
                        server.kill()
                        server.join(timeout=2)
                    server = start_server()
                    _wait_for_api()
                    restart_count += 1
                    control_path.write_text(
                        json.dumps({"restart": False, "restart_count": restart_count}),
                        encoding="utf-8",
                    )
                time.sleep(0.1)
        finally:
            server.terminate()
            server.join(timeout=5)
            if server.is_alive():
                server.kill()
                server.join(timeout=2)


if __name__ == "__main__":
    main()
