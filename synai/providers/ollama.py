from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from synai.models import ChatEvent, Message, ModelInfo
from synai.providers.base import ProviderError


class OllamaProvider:
    def __init__(self, base_url: str, timeout: float = 1200) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10))

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await (
                self.client.get(self.base_url + path) if payload is None
                else self.client.post(self.base_url + path, json=payload)
            )
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Ollama {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ProviderError("Ollama response must be a JSON object")
        if value.get("error"):
            raise ProviderError(str(value["error"]))
        return value

    async def list_models(self) -> list[ModelInfo]:
        result = await self._request("/api/tags")
        values = result.get("models")
        if not isinstance(values, list):
            raise ProviderError("Ollama response has no models list")
        models = []
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("name"), str):
                raise ProviderError("Ollama returned an invalid model entry")
            models.append(ModelInfo(value["name"]))
        return models

    async def capabilities(self, name: str) -> ModelInfo:
        result = await self._request("/api/show", {"model": name})
        capabilities = result.get("capabilities", [])
        if not isinstance(capabilities, list):
            raise ProviderError("Invalid model capability metadata")
        return ModelInfo(name, tools="tools" in capabilities, thinking="thinking" in capabilities)

    async def chat(
        self, model: str, messages: list[Message], tools: list[dict[str, Any]],
    ) -> AsyncIterator[ChatEvent]:
        payload: dict[str, Any] = {
            "model": model, "messages": [message.wire() for message in messages], "stream": True,
        }
        if tools:
            payload["tools"] = tools
        done = False
        try:
            async with self.client.stream("POST", self.base_url + "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    if len(line) > 4 * 1024 * 1024:
                        raise ProviderError("Ollama stream event exceeds 4 MiB")
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ProviderError("Ollama stream event must be an object")
                    if value.get("error"):
                        raise ProviderError(str(value["error"]))
                    message = value.get("message", {})
                    if not isinstance(message, dict):
                        raise ProviderError("Ollama message must be an object")
                    content, thinking = message.get("content", ""), message.get("thinking", "")
                    calls = message.get("tool_calls", [])
                    if not isinstance(content, str) or not isinstance(thinking, str) or not isinstance(calls, list):
                        raise ProviderError("Malformed chat content/thinking/tool calls")
                    if any(not isinstance(call, dict) for call in calls):
                        raise ProviderError("Malformed tool call")
                    done = value.get("done") is True
                    yield ChatEvent(content, thinking, calls, done)
                    if done:
                        break
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Ollama chat failed: {exc}") from exc
        if not done:
            raise ProviderError("Ollama stream ended before the final marker")
