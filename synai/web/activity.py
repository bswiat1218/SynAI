from __future__ import annotations

import json
import sqlite3
import time
from typing import Literal

from pydantic import Field, StrictInt

from synai.web.database import MetadataDatabase
from synai.web.schemas import StrictSchema


ProjectActivityType = Literal[
    "project_snapshot",
    "resynchronization_required",
    "project_created",
    "workspace_binding_created",
    "workspace_binding_revoked",
    "device_revoked",
    "device_connected",
    "device_disconnected",
    "snapshot_committed",
    "snapshot_expired",
]


class ProjectActivityEnvelope(StrictSchema):
    schema_version: Literal[1] = 1
    event_id: StrictInt = Field(ge=0)
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    type: ProjectActivityType
    created_at: StrictInt = Field(ge=0)
    payload: dict[str, str | int | bool | None] = Field(max_length=8)


def append_project_activity(
    connection: sqlite3.Connection,
    project_id: str,
    event_type: ProjectActivityType,
    payload: dict[str, str | int | bool | None],
    created_at: int,
) -> dict[str, object]:
    row = connection.execute(
        "SELECT sequence FROM project_activity_cursors WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    sequence = 1 if row is None else int(row["sequence"]) + 1
    envelope = ProjectActivityEnvelope(
        schema_version=1,
        event_id=sequence,
        project_id=project_id,
        type=event_type,
        created_at=created_at,
        payload=payload,
    ).model_dump()
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > ProjectActivityStore.MAX_EVENT_BYTES:
        raise ValueError("Project activity event exceeds the supported size.")
    connection.execute(
        "INSERT INTO project_activity_cursors(project_id, sequence) VALUES (?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET sequence = excluded.sequence",
        (project_id, sequence),
    )
    connection.execute(
        "INSERT INTO project_activity_events(project_id, sequence, created_at, event_json) "
        "VALUES (?, ?, ?, ?)",
        (project_id, sequence, created_at, encoded),
    )
    connection.execute(
        "DELETE FROM project_activity_events WHERE project_id = ? AND sequence NOT IN "
        "(SELECT sequence FROM project_activity_events WHERE project_id = ? "
        "ORDER BY sequence DESC LIMIT ?)",
        (project_id, project_id, ProjectActivityStore.MAX_RETAINED_EVENTS),
    )
    return envelope


class ProjectActivityStore:
    MAX_EVENT_BYTES = 4096
    MAX_RETAINED_EVENTS = 1024
    MAX_REPLAY_EVENTS = 128
    MAX_SUBSCRIBERS_PER_PROJECT = 16
    POLL_INTERVAL_SECONDS = 0.5

    def __init__(self, database: MetadataDatabase) -> None:
        self.database = database
        self._subscribers: dict[str, int] = {}

    def acquire(self, project_id: str) -> bool:
        count = self._subscribers.get(project_id, 0)
        if count >= self.MAX_SUBSCRIBERS_PER_PROJECT:
            return False
        self._subscribers[project_id] = count + 1
        return True

    def release(self, project_id: str) -> None:
        count = self._subscribers.get(project_id, 0)
        if count <= 1:
            self._subscribers.pop(project_id, None)
        else:
            self._subscribers[project_id] = count - 1

    def read_after(
        self, project_id: str, after: int, limit: int = MAX_REPLAY_EVENTS,
    ) -> tuple[list[dict[str, object]], int, bool]:
        if type(after) is not int or after < 0 or not 1 <= limit <= self.MAX_REPLAY_EVENTS:
            raise ValueError("Project activity cursor or limit is invalid.")
        with self.database.connect() as connection:
            cursor_row = connection.execute(
                "SELECT sequence FROM project_activity_cursors WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            current = 0 if cursor_row is None else int(cursor_row["sequence"])
            first_row = connection.execute(
                "SELECT min(sequence) FROM project_activity_events WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            first = first_row[0]
            gap = after > current or (
                after < current and (first is None or after < int(first) - 1)
            )
            if gap:
                return [], current, True
            rows = connection.execute(
                "SELECT event_json FROM project_activity_events "
                "WHERE project_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (project_id, after, limit + 1),
            ).fetchall()
        if len(rows) > limit:
            return [], current, True
        return [json.loads(row["event_json"]) for row in rows], current, False

    def list_recent(
        self, project_id: str, limit: int,
    ) -> tuple[list[dict[str, object]], int]:
        if not 1 <= limit <= 256:
            raise ValueError("Project activity limit is invalid.")
        with self.database.connect() as connection:
            cursor_row = connection.execute(
                "SELECT sequence FROM project_activity_cursors WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            current = 0 if cursor_row is None else int(cursor_row["sequence"])
            rows = connection.execute(
                "SELECT event_json FROM project_activity_events "
                "WHERE project_id = ? ORDER BY sequence DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [json.loads(row["event_json"]) for row in reversed(rows)], current

    @staticmethod
    def resync_event(project_id: str, sequence: int) -> dict[str, object]:
        return ProjectActivityEnvelope(
            schema_version=1,
            event_id=sequence,
            project_id=project_id,
            type="resynchronization_required",
            created_at=int(time.time()),
            payload={"cursor": sequence},
        ).model_dump()

    def snapshot_event(
        self, project_id: str, sequence: int, project_name: str, project_status: str,
    ) -> dict[str, object]:
        return ProjectActivityEnvelope(
            schema_version=1,
            event_id=sequence,
            project_id=project_id,
            type="project_snapshot",
            created_at=int(time.time()),
            payload={
                "snapshot": True,
                "project_name": project_name,
                "project_status": project_status,
                "resync": True,
            },
        ).model_dump()
