from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from synai.providers.ollama import OllamaProvider
from synai.web.app import create_app
from synai.web.config import WebConfig


ENABLED = os.environ.get("SYNAI_RUN_OLLAMA_SMOKE") == "1"
ORIGIN = "http://127.0.0.1:8765"
PASSWORD = "disposable Ollama smoke credential"


@unittest.skipUnless(
    ENABLED,
    "Set SYNAI_RUN_OLLAMA_SMOKE=1, SYNAI_OLLAMA_SMOKE_URL, and "
    "SYNAI_OLLAMA_SMOKE_MODEL to opt in.",
)
class OllamaWebChatSmokeTests(unittest.TestCase):
    def test_discovery_stream_persistence_and_restart(self) -> None:
        endpoint = os.environ["SYNAI_OLLAMA_SMOKE_URL"]
        model = os.environ["SYNAI_OLLAMA_SMOKE_MODEL"]
        with tempfile.TemporaryDirectory(prefix="synai-ollama-smoke-") as directory:
            root = Path(directory) / "data"
            config = WebConfig(
                data_root=root,
                workspace_mounts=(),
                initial_password=PASSWORD,
                ollama_url=endpoint,
                public_origin=ORIGIN,
            )
            provider = OllamaProvider(endpoint)
            app = create_app(config, provider=provider)
            with TestClient(app) as client:
                login = client.post(
                    "/api/v1/auth/login",
                    json={"password": PASSWORD},
                    headers={"Origin": ORIGIN},
                )
                self.assertEqual(login.status_code, 200, login.text)
                csrf = login.json()["csrf_token"]
                models = client.get("/api/v1/models")
                self.assertEqual(models.status_code, 200, models.text)
                self.assertIn(model, {entry["name"] for entry in models.json()["models"]})
                created = client.post(
                    "/api/v1/chat/sessions",
                    json={"model": model},
                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
                )
                self.assertEqual(created.status_code, 201, created.text)
                conversation_id = created.json()["id"]
                turn = client.post(
                    f"/api/v1/chat/sessions/{conversation_id}/turns",
                    json={"model": model, "prompt": "Reply with the single word OK."},
                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
                )
                self.assertEqual(turn.status_code, 202, turn.text)
                streamed: list[str] = []
                completed = False
                with client.websocket_connect(
                    f"/api/v1/events/v1/chat/{conversation_id}",
                    headers={"Origin": ORIGIN},
                ) as websocket:
                    for _ in range(256):
                        event = websocket.receive_json()
                        if event["type"] == "content_delta":
                            streamed.append(event["payload"]["text"])
                        if event["type"] == "turn_completed":
                            completed = True
                            break
                self.assertTrue(completed, "Ollama stream did not emit a completion event")
                self.assertTrue("".join(streamed).strip())
                history = client.get(f"/api/v1/chat/sessions/{conversation_id}")
                self.assertEqual(history.status_code, 200, history.text)
                saved_assistant = "".join(
                    message["content"] for message in history.json()["messages"]
                    if message["role"] == "assistant"
                )
                self.assertTrue(saved_assistant.strip())

            restarted = create_app(config, provider=OllamaProvider(endpoint))
            with TestClient(restarted) as client:
                login = client.post(
                    "/api/v1/auth/login",
                    json={"password": PASSWORD},
                    headers={"Origin": ORIGIN},
                )
                self.assertEqual(login.status_code, 200, login.text)
                reopened = client.get(f"/api/v1/chat/sessions/{conversation_id}")
                self.assertEqual(reopened.status_code, 200, reopened.text)
                self.assertEqual(
                    "".join(message["content"] for message in reopened.json()["messages"]
                            if message["role"] == "assistant"),
                    saved_assistant,
                )


if __name__ == "__main__":
    unittest.main()
