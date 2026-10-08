from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from synai.coding_agent.state import AgentCheckpoint
from synai.config import ConversationEnvironment


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ModelInfo:
    name: str
    tools: bool = False
    thinking: bool = False
    capability_error: str | None = None


@dataclass
class ChatEvent:
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False


@dataclass
class GenerationSource:
    model: str
    endpoint: str
    provider: str = "ollama"
    legacy: bool = False


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_name: str | None = None
    status: str = "complete"
    created_at: str = field(default_factory=now)
    source: GenerationSource | None = None

    def wire(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.thinking:
            result["thinking"] = self.thinking
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        if self.tool_name:
            result["tool_name"] = self.tool_name
        return result


@dataclass
class Activity:
    kind: str
    text: str
    created_at: str = field(default_factory=now)


@dataclass
class Session:
    model: str
    endpoint: str
    workspace: str
    session_id: str = field(default_factory=lambda: uuid4().hex)
    title: str = "New conversation"
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    schema_version: int = 1
    container_id: str | None = None
    messages: list[Message] = field(default_factory=list)
    activity: list[Activity] = field(default_factory=list)
    state: str = "idle"
    limits: dict[str, int | float] = field(default_factory=dict)
    environment: ConversationEnvironment | None = None
    managed_workspace_created: bool = False
    legacy_request_timeout: float | None = None
    agent_checkpoint: AgentCheckpoint | None = None

    def set_environment(self, environment: ConversationEnvironment) -> None:
        environment.validate()
        self.environment = environment
        self.schema_version = 6 if self.agent_checkpoint is not None else 5
        self.workspace = environment.workspace
        self.limits = {
            "command_timeout": environment.command_timeout, "output_bytes": environment.output_bytes,
            "tool_budget": environment.tool_budget,
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.agent_checkpoint is None:
            result.pop("agent_checkpoint")
        return result
