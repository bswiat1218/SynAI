from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from synai.models import ChatEvent, Message, ModelInfo


class ProviderError(Exception):
    pass


class ModelProvider(Protocol):
    async def list_models(self) -> list[ModelInfo]: ...

    async def capabilities(self, name: str) -> ModelInfo: ...

    def chat(self, model: str, messages: list[Message], tools: list[dict[str, Any]]) -> AsyncIterator[ChatEvent]: ...
