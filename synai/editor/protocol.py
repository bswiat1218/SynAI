from __future__ import annotations

import json
import re
from typing import Any

VERSION = 1
MAX_MESSAGE = 256 * 1024
COLORS = ("foreground", "background", "surface", "panel", "primary", "secondary",
          "accent", "warning", "error", "success")


class EditorError(Exception):
    pass


def encode(message: dict[str, Any]) -> bytes:
    data = (json.dumps({**message, "version": VERSION}, ensure_ascii=True) + "\n").encode()
    if len(data) > MAX_MESSAGE:
        raise EditorError("Editor message exceeds transport limit")
    return data


def decode(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_MESSAGE:
        raise EditorError("Editor message exceeds transport limit")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise EditorError(f"Invalid editor message: {exc}") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != VERSION:
        raise EditorError("Unsupported editor protocol")
    if not isinstance(value.get("type"), str):
        raise EditorError("Editor message needs a type")
    kind = value["type"]
    if kind not in {"launch", "focus", "theme", "close", "reply", "error", "closed"}:
        raise EditorError("Unknown editor message type")
    if kind in {"launch", "focus", "theme", "close", "reply"} and (
        type(value.get("id")) is not int or value["id"] < 1
    ):
        raise EditorError("Editor message needs a positive request identity")
    if kind == "reply" and type(value.get("ok")) is not bool:
        raise EditorError("Editor reply needs an explicit result")
    if kind == "reply" and not value["ok"] and not isinstance(value.get("error"), str):
        raise EditorError("Failed editor reply needs an error message")
    if "cancelled" in value and type(value["cancelled"]) is not bool:
        raise EditorError("Invalid editor cancellation result")
    if kind == "error" and not isinstance(value.get("error"), str):
        raise EditorError("Editor error needs a message")
    return value


def palette(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("dark")) is not bool:
        raise EditorError("Invalid editor palette")
    result: dict[str, Any] = {"dark": value["dark"]}
    for key in COLORS:
        color = value.get(key)
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise EditorError(f"Invalid editor color: {key}")
        result[key] = color
    return result
