from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from synai.web.activity import append_project_activity
from synai.web.database import MetadataDatabase


@dataclass
class ActiveDeviceConnection:
    device_id: str
    websocket: WebSocket
    connected_at: int
    next_server_sequence: int = 1
    pending_operations: dict[str, dict[str, object]] | None = None


class DeviceConnectionManager:
    """Tracks only current authenticated sockets; connections are never durable authority."""

    MAX_CONNECTIONS = 256
    HEARTBEAT_TIMEOUT_SECONDS = 45

    def __init__(self, database: MetadataDatabase) -> None:
        self.database = database
        self._active: dict[str, ActiveDeviceConnection] = {}

    def connected(self, device_id: str) -> bool:
        return device_id in self._active

    def connected_at(self, device_id: str) -> int | None:
        connection = self._active.get(device_id)
        return None if connection is None else connection.connected_at

    async def register(self, device_id: str, websocket: WebSocket) -> ActiveDeviceConnection:
        if device_id not in self._active and len(self._active) >= self.MAX_CONNECTIONS:
            raise RuntimeError("Active device connection capacity is full.")
        previous = self._active.get(device_id)
        current = ActiveDeviceConnection(device_id, websocket, int(time.time()), pending_operations={})
        self._active[device_id] = current
        if previous is not None:
            try:
                await previous.websocket.close(code=4001, reason="Replaced by a newer authenticated connection")
            except (RuntimeError, WebSocketDisconnect):
                pass
        self._record_event(device_id, "device_connected", current.connected_at)
        return current

    async def send(self, connection: ActiveDeviceConnection, message_type: str, payload: dict[str, object]) -> None:
        if self._active.get(connection.device_id) is not connection:
            raise RuntimeError("Device connection is no longer active.")
        sequence = connection.next_server_sequence
        connection.next_server_sequence += 1
        await asyncio.wait_for(connection.websocket.send_json({
            "schema_version": 1,
            "type": message_type,
            "device_id": connection.device_id,
            "sequence": sequence,
            "payload": payload,
        }), timeout=5)

    async def request_snapshot(
        self,
        device_id: str,
        project_id: str,
        binding_id: str,
        expires_at: int,
    ) -> str:
        connection = self._active.get(device_id)
        if connection is None:
            raise RuntimeError("Device is not currently connected.")
        if connection.pending_operations is not None:
            for pending_id, pending in tuple(connection.pending_operations.items()):
                if pending["expires_at"] <= int(time.time()):
                    connection.pending_operations.pop(pending_id, None)
        if connection.pending_operations is None or len(connection.pending_operations) >= 8:
            raise RuntimeError("Device snapshot request capacity is full.")
        operation_id = secrets.token_hex(16)
        operation = {
            "project_id": project_id,
            "binding_id": binding_id,
            "expires_at": expires_at,
        }
        connection.pending_operations[operation_id] = operation
        try:
            await self.send(connection, "snapshot_request", {
                "operation_id": operation_id,
                **operation,
                "capability": "snapshot-v1",
            })
        except BaseException:
            connection.pending_operations.pop(operation_id, None)
            raise
        return operation_id

    def validate_operation_message(
        self,
        connection: ActiveDeviceConnection,
        payload: dict[str, object],
        message_type: str,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else now
        if set(payload) != {"operation_id", "project_id", "binding_id", "status"}:
            return False
        operation_id = payload["operation_id"]
        if not isinstance(operation_id, str) or connection.pending_operations is None:
            return False
        operation = connection.pending_operations.get(operation_id)
        if (
            operation is None
            or operation["expires_at"] <= timestamp
            or payload["project_id"] != operation["project_id"]
            or payload["binding_id"] != operation["binding_id"]
            or message_type == "snapshot_progress"
            and payload["status"] not in {"awaiting_consent", "uploading"}
            or message_type == "snapshot_completed" and payload["status"] != "completed"
            or message_type == "snapshot_failed"
            and payload["status"] not in {"failed", "declined"}
        ):
            return False
        if message_type in {"snapshot_completed", "snapshot_failed"}:
            connection.pending_operations.pop(operation_id, None)
        return True

    async def unregister(self, connection: ActiveDeviceConnection) -> None:
        if self._active.get(connection.device_id) is connection:
            self._active.pop(connection.device_id, None)
            self._record_event(connection.device_id, "device_disconnected", int(time.time()))

    async def disconnect_device(self, device_id: str) -> None:
        connection = self._active.get(device_id)
        if connection is None:
            return
        self._active.pop(device_id, None)
        try:
            await connection.websocket.close(code=4003, reason="Device authorization revoked")
        except (RuntimeError, WebSocketDisconnect):
            pass
        self._record_event(device_id, "device_disconnected", int(time.time()))

    async def close_all(self) -> None:
        connections = tuple(self._active.values())
        self._active.clear()
        for connection in connections:
            try:
                await connection.websocket.close(code=1001, reason="SynAI Core is shutting down")
            except (RuntimeError, WebSocketDisconnect):
                pass
            self._record_event(connection.device_id, "device_disconnected", int(time.time()))

    def _record_event(self, device_id: str, event_type: str, timestamp: int) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                projects = connection.execute(
                    "SELECT DISTINCT project_id FROM workspace_bindings "
                    "WHERE device_id = ? AND state = 'active' LIMIT 512",
                    (device_id,),
                ).fetchall()
                for project in projects:
                    append_project_activity(
                        connection,
                        project["project_id"],
                        event_type,
                        {"device_id": device_id},
                        timestamp,
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
