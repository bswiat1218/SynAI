from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from synai.web.app import create_app
from synai.web.config import WebConfig


class OpenApiContractTests(unittest.TestCase):
    def test_checked_in_openapi_is_generated_from_fastapi_contract(self) -> None:
        class Provider:
            async def list_models(self):
                return []

            async def capabilities(self, name: str):
                raise NotImplementedError

            async def chat(self, model: str, messages: list, tools: list):
                raise NotImplementedError

        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(
                WebConfig(Path(temporary), ()),
                provider=Provider(),
            )
        checked_in = json.loads(
            (Path(__file__).resolve().parents[1] / "web" / "openapi.json").read_text(),
        )
        self.assertEqual(app.openapi(), checked_in)
