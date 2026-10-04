from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    execution_mode: Literal["sandbox", "host"] = "sandbox"
    ollama_url: str = field(default_factory=lambda: (
        os.getenv("OLLAMA_URL") or os.getenv("OLLAMA_HOST") or "http://localhost:11434"
    ).rstrip("/"))
    request_timeout: float = field(default_factory=lambda: float(
        os.getenv("AGENT_REQUEST_TIMEOUT", os.getenv("BENCHMARK_REQUEST_TIMEOUT", "1200"))
    ))
    history_dir: Path = field(default_factory=lambda: Path(
        str(Path.home() / ".synai")
    ).expanduser().absolute())
    runtime: str = field(default_factory=lambda: os.getenv("AGENT_RUNTIME", "docker"))
    image: str = field(default_factory=lambda: os.getenv("AGENT_IMAGE", "python:3.12-slim"))
    command_timeout: float = 60
    output_bytes: int = 1024 * 1024
    tool_budget: int = 20
    memory: str = "1g"
    cpus: float = 2
    pids: int = 128

    def validate(self) -> None:
        if self.execution_mode not in {"sandbox", "host"}:
            raise ValueError("Execution mode must be sandbox or host")
        for value in (self.ollama_url, self.runtime, self.image, self.memory):
            if not isinstance(value, str):
                raise ValueError("Endpoint, runtime, image and memory must be strings")
        url = urlsplit(self.ollama_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise ValueError("Ollama URL must be an absolute HTTP(S) URL without credentials")
        for value in (self.request_timeout, self.command_timeout, self.cpus):
            try:
                valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("Timeouts and CPU limit must be finite and positive")
        if self.runtime not in {"docker", "podman"}:
            raise ValueError("Runtime must be docker or podman")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (
            self.output_bytes, self.tool_budget, self.pids,
        )) or self.output_bytes < 1024 or self.tool_budget < 1 or self.pids < 1:
            raise ValueError("Tool output/budget/PID limits must be positive (output >= 1024 bytes)")
        if not self.image or self.image.startswith("-") or any(char.isspace() for char in self.image):
            raise ValueError("Invalid image reference")
        if not re.fullmatch(r"[1-9]\d*(?:[bkmgBKMG])?", self.memory):
            raise ValueError("Memory must be a positive byte count or integer with b/k/m/g suffix")


@dataclass(frozen=True)
class ConversationEnvironment:
    workspace: str
    runtime: str
    image: str
    command_timeout: float
    output_bytes: int
    tool_budget: int
    memory: str
    cpus: float
    pids: int
    execution_mode: Literal["sandbox", "host"] = "sandbox"

    @classmethod
    def from_settings(cls, settings: Settings, workspace: Path) -> ConversationEnvironment:
        return cls(str(workspace.expanduser().resolve()), settings.runtime, settings.image,
                   settings.command_timeout, settings.output_bytes,
                   settings.tool_budget, settings.memory, settings.cpus, settings.pids, settings.execution_mode)

    def settings(self, launch: Settings) -> Settings:
        return replace(
            launch, runtime=self.runtime, image=self.image,
            command_timeout=self.command_timeout,
            output_bytes=self.output_bytes, tool_budget=self.tool_budget,
            memory=self.memory, cpus=self.cpus, pids=self.pids,
            execution_mode=self.execution_mode,
        )

    def validate(self) -> None:
        if not isinstance(self.workspace, str) or not Path(self.workspace).is_absolute():
            raise ValueError("Conversation workspace must be an absolute path")
        Settings(
            history_dir=Path.home(), ollama_url="http://localhost:11434", request_timeout=1200,
            runtime=self.runtime, image=self.image, command_timeout=self.command_timeout,
            output_bytes=self.output_bytes, tool_budget=self.tool_budget, memory=self.memory, cpus=self.cpus, pids=self.pids,
            execution_mode=self.execution_mode,
        ).validate()
