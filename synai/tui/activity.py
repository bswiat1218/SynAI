from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from rich.text import Text
from rich.text import Span

from synai.models import Activity


CYAN = "#00f5ff"
PINK = "#ff709e"
AMBER = "#ffe66d"
MUTED = "#bba5d9"
TITLES = {
    "list_files": "List directory",
    "read_file": "Read file",
    "write_file": "Write file",
    "patch_file": "Patch file",
    "delete_file": "Delete file",
    "terminal": "Run command",
    "fetch_url": "Fetch URL",
}
HEADINGS = {
    "approval": "Approval",
    "limit": "Tool limit",
    "cancel": "Cancelled",
    "resume": "Conversation recovery",
    "error": "Error",
}


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[Preview shortened; full bounded data remains in history]"


def _section(text: Text, label: str, value: str, limit: int = 3000, style: str = "") -> None:
    text.append(f"\n{label}\n", style=f"bold {MUTED}")
    text.append(_bounded(value, limit) if value else "(no output)", style=style)


def _preview(text: Text, label: str, value: Any) -> None:
    if not isinstance(value, str):
        _section(text, label, "Invalid or missing text field", style=PINK)
        return
    lines = len(value.splitlines())
    size = len(value.encode("utf-8", errors="replace"))
    _section(text, f"{label} ({lines} lines, {size} UTF-8 bytes)", value, limit=2000)


def _field(text: Text, label: str, value: Any) -> None:
    if not isinstance(value, str):
        text.append(f"\n{label}: invalid or missing text field", style=PINK)
    else:
        text.append(f"\n{label}: {_bounded(value, 1500)}")


def _request(text: Text, name: str, data: dict[str, Any]) -> None:
    if name == "terminal":
        _field(text, "Command", data.get("command"))
        _field(text, "Working directory", data.get("cwd"))
    elif name == "fetch_url":
        _field(text, "URL", data.get("url"))
    else:
        _field(text, "Directory" if name == "list_files" else "File", data.get("path"))
        if name == "write_file":
            _preview(text, "Proposed content", data.get("content"))
        elif name == "patch_file":
            _preview(text, "Replace", data.get("old"))
            _preview(text, "With", data.get("new"))


def _result(text: Text, name: str, data: dict[str, Any]) -> None:
    invalid_flags = [key for key in ("ok", "denied", "timed_out", "truncated")
                     if key in data and not isinstance(data[key], bool)]
    if invalid_flags:
        status, color = "Unknown result - invalid status fields: " + ", ".join(invalid_flags), AMBER
    elif data.get("denied") is True:
        status, color = "Denied by you - not executed", AMBER
    elif data.get("timed_out") is True:
        status, color = "Timed out", PINK
    elif data.get("truncated") is True and name == "terminal":
        status, color = "Stopped at output limit", AMBER
    elif data.get("ok") is True:
        status, color = "Succeeded", CYAN
    elif data.get("ok") is False:
        status, color = "Failed", PINK
    else:
        status, color = "Unknown result - missing or invalid success status", AMBER
    if isinstance(data.get("error"), str) and data["error"].startswith("Interrupted;"):
        status, color = "Interrupted - not replayed", AMBER
    text.append(f"\n{status}", style=f"bold {color}")
    if "error" in data:
        _field(text, "Reason", data["error"])
    if name == "terminal":
        if "exit_code" in data:
            code = data["exit_code"]
            text.append(f"\nExit code: {code}" if isinstance(code, int) and not isinstance(code, bool)
                        else "\nExit code: unavailable or invalid")
        if "duration" in data:
            duration = data["duration"]
            seconds: float | None = None
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                try:
                    seconds = float(duration)
                except OverflowError:
                    seconds = None
            valid = seconds is not None and math.isfinite(seconds) and seconds >= 0
            text.append(f"\nElapsed: {seconds:.2f}s" if valid else "\nElapsed: invalid duration")
        for key, label, style in (("stdout", "STDOUT", ""), ("stderr", "STDERR", PINK)):
            if key in data or data.get("ok") is True:
                value = data.get(key, "")
                _section(text, label, value if isinstance(value, str) else "Invalid output field", style=style)
    elif name == "list_files" and "entries" in data:
        entries = data["entries"]
        if not isinstance(entries, list):
            _section(text, "Directory entries", "Invalid directory listing", style=PINK)
        else:
            rendered = []
            for entry in entries[:100]:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    rendered.append("[Invalid directory entry]")
                    continue
                marker = " [symlink]" if entry.get("symlink") else "/" if entry.get("directory") else ""
                rendered.append(entry["name"] + marker)
            if len(entries) > 100:
                rendered.append("[More entries in history]")
            _section(text, f"Directory entries ({len(entries)})", "\n".join(rendered) or "(empty directory)")
    elif name == "read_file" and "content" in data:
        _preview(text, "File content", data["content"])
    elif name == "fetch_url":
        if "status" in data:
            code = data["status"]
            valid = isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599
            text.append(f"\nHTTP status: {code}" if valid else "\nHTTP status: invalid or unavailable")
        if "text" in data:
            _preview(text, "Response", data["text"])
    elif "path" in data:
        _field(text, "File", data["path"])
    if data.get("truncated") is True:
        text.append("\nOutput was truncated at the configured byte limit.", style=AMBER)
    known = {"ok", "denied", "error", "exit_code", "duration", "stdout", "stderr", "timed_out",
             "truncated", "entries", "content", "sha256", "status", "text", "path"}
    extra = {key: value for key, value in data.items() if key not in known}
    if extra:
        _section(text, "Additional result fields", json.dumps(extra, ensure_ascii=False), limit=1500)


def _themed(text: Text, enabled: bool) -> Text:
    if enabled:
        colors = {CYAN: "synai.primary", PINK: "synai.error", AMBER: "synai.warning", MUTED: "synai.muted"}
        text.style = colors.get(str(text.style), text.style)
        text.spans = [
            Span(span.start, span.end, " ".join(colors.get(part, part) for part in str(span.style).split()))
            for span in text.spans
        ]
    return text


def format_activity(activity: Activity, *, themed: bool = False) -> Text:
    """Present stored activity without changing its raw history representation."""
    try:
        timestamp = datetime.fromisoformat(activity.created_at)
        if timestamp.tzinfo is None:
            raise ValueError("Missing timezone")
        clock = timestamp.astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        clock = f"Invalid timestamp: {_bounded(str(activity.created_at), 100)}"
    text = Text(f"{clock} // ", style=MUTED)
    if activity.kind not in {"tool", "result"}:
        heading = HEADINGS.get(activity.kind, f"Unformatted activity ({activity.kind})")
        color = PINK if activity.kind == "error" else AMBER if activity.kind in {"approval", "limit", "cancel"} else CYAN
        text.append(heading, style=f"bold {color}")
        detail = activity.text
        if activity.kind == "approval":
            prefix, separator, name = detail.partition(": ")
            if separator and prefix in {"Requested", "Allowed", "Denied"} and name in TITLES:
                detail = f"{prefix}: {TITLES[name]}"
        text.append("\n" + _bounded(detail, 4000))
        return _themed(text, themed)
    name, separator, payload = activity.text.partition(": ")
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        data = None
    if not separator or not isinstance(data, dict) or name not in TITLES:
        text.append("Unformatted tool activity", style=f"bold {AMBER}")
        text.append("\n" + _bounded(activity.text, 4000))
        return _themed(text, themed)
    text.append(TITLES[name] + (" // Requested" if activity.kind == "tool" else " // Result"),
                style=f"bold {CYAN}")
    if activity.kind == "tool":
        _request(text, name, data)
    else:
        _result(text, name, data)
    if len(text) > 16000:
        text = text[:16000]
        text.append("\n[Activity shortened; full bounded data remains in history]", style=AMBER)
    return _themed(text, themed)
