from __future__ import annotations

import json
from pathlib import Path

from synai.web.app import create_app
from synai.web.config import WebConfig


class _SchemaProvider:
    async def list_models(self) -> list:
        return []

    async def capabilities(self, name: str):
        raise NotImplementedError

    async def chat(self, model: str, messages: list, tools: list):
        raise NotImplementedError


def main() -> None:
    app = create_app(
        WebConfig(Path.home() / ".synai", ()),
        provider=_SchemaProvider(),
    )
    print(json.dumps(app.openapi(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
