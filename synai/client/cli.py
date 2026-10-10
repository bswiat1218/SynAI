from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import json
import os
import platform
import secrets
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.exceptions import ConnectionClosed, WebSocketException

from synai.client import __version__
from synai.client.protocol import (
    CONNECTION_PATH,
    PROTOCOL_VERSION,
    ClientProtocolError,
    api_client,
    canonical_json,
    enrollment_message,
    load_private_key,
    server_origin,
    sign_connection_message,
    signed_http_request,
)
from synai.client.state import ClientState, ClientStateError
from synai.client.workspace import (
    WorkspaceError,
    WorkspaceRegistry,
    capture_workspace,
)


CAPABILITIES: dict[str, object] = {
    "agent_version": f"synai-client/{__version__}",
    "platform": "linux",
    "architecture": platform.machine()[:64],
    "features": ["snapshot-v1", "status-v1"],
}
CHUNK_BYTES = 256 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synai-client", description="Unprivileged SynAI Linux Client Agent")
    parser.add_argument("--version", action="version", version=f"synai-client {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="Configure the SynAI Core URL")
    setup.add_argument("--server", required=True)

    sub.add_parser("pair", help="Pair this computer using a short-lived administrator challenge")
    sub.add_parser("status", help="Authenticate and show current Core connection status")
    sub.add_parser("connect", help="Maintain the authenticated outbound Core connection")

    workspace = sub.add_parser("workspace", help="Manage locally authorized workspaces")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    add = workspace_commands.add_parser("add", help="Authorize a local directory for a server binding")
    add.add_argument("--project-id", required=True)
    add.add_argument("--binding-id", required=True)
    add.add_argument("--alias", required=True)
    add.add_argument("directory", type=Path)
    remove = workspace_commands.add_parser("remove", help="Immediately revoke a local workspace binding")
    remove.add_argument("--binding-id", required=True)
    workspace_commands.add_parser("list", help="List local workspace aliases")

    snapshot = sub.add_parser("snapshot", help="Preview and explicitly approve a read-only snapshot upload")
    snapshot.add_argument("--binding-id", required=True)

    revoke = sub.add_parser("disconnect", help="Remove local device credentials and stop the client")
    revoke.add_argument("--forget", action="store_true", help="Delete local credentials after disconnect")
    sub.add_parser("diagnose", help="Show non-secret client and transport diagnostics")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    state = ClientState()
    try:
        state.initialize()
        if args.command == "setup":
            origin = server_origin(args.server.rstrip("/"))
            state.write_config({"schema_version": 1, "server_url": origin})
            print(f"Configured Core: {origin}")
        elif args.command == "pair":
            _pair(state)
        elif args.command == "status":
            asyncio.run(_run_connection(state, once=True))
        elif args.command == "connect":
            asyncio.run(_run_connection(state, once=False))
        elif args.command == "workspace":
            _workspace_command(state, args)
        elif args.command == "snapshot":
            _snapshot(state, args.binding_id)
        elif args.command == "disconnect":
            _disconnect(state, args.forget)
        elif args.command == "diagnose":
            _diagnose(state)
    except (ClientStateError, ClientProtocolError, WorkspaceError, OSError, ValueError) as exc:
        print(f"synai-client: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def _pair(state: ClientState) -> None:
    config = _config(state)
    server_url = str(config["server_url"])
    challenge_id = input("Pairing challenge ID: ").strip()
    if len(challenge_id) != 32 or any(char not in "0123456789abcdef" for char in challenge_id):
        raise ClientProtocolError("Pairing challenge ID is invalid.")
    challenge_secret = getpass.getpass("Pairing challenge secret: ")
    private = Ed25519PrivateKey.generate()
    public_key = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    signature = private.sign(enrollment_message(
        challenge_id, challenge_secret, public_key, PROTOCOL_VERSION, CAPABILITIES,
    ))
    print(f"Pair this Linux client with SynAI Core at {server_url}?")
    if input("Type PAIR to confirm locally: ").strip() != "PAIR":
        print("Pairing cancelled; no credentials were sent.")
        return
    body = canonical_json({
        "challenge_id": challenge_id,
        "challenge_secret": challenge_secret,
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": CAPABILITIES,
        "signature": base64.b64encode(signature).decode("ascii"),
    })
    with api_client(server_url) as client:
        try:
            response = client.post("/api/v1/device-enrollments", content=body, headers={"Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise ClientProtocolError("Pairing request could not reach SynAI Core.") from exc
    if len(response.content) > 8192:
        raise ClientProtocolError("Pairing response exceeded its safety limit.")
    if not response.is_success:
        raise _http_error(response)
    result = response.json()
    if (
        not isinstance(result, dict)
        or result.get("state") != "pending"
        or not _opaque_id(result.get("device_id"))
        or not isinstance(result.get("credential"), str)
    ):
        raise ClientProtocolError("Core returned an invalid pairing response.")
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    state.write_identity({
        "schema_version": 1,
        "device_id": result["device_id"],
        "credential": result["credential"],
        "credential_expires_at": result["credential_expires_at"],
        "private_key_pem": private_pem,
        "server_url": server_url,
        "protocol_version": PROTOCOL_VERSION,
        "created_at": int(time.time()),
    })
    print(f"Pairing is pending administrator authorization. Device ID: {result['device_id']}")
    print("The private key remains in the local mode-0600 identity file.")


def _workspace_command(state: ClientState, args: argparse.Namespace) -> None:
    registry = WorkspaceRegistry(state)
    if args.workspace_command == "add":
        record = registry.add(args.project_id, args.binding_id, args.alias, args.directory)
        print(f"Authorized local workspace '{record['alias']}' for project {record['project_id']}.")
    elif args.workspace_command == "remove":
        registry.remove(args.binding_id)
        print("Local workspace authorization revoked. New snapshots are blocked.")
    else:
        for item in registry.list():
            print(f"{item.get('alias')}  project={item.get('project_id')}  binding={item.get('binding_id')}  status={item.get('status')}")
        if not registry.list():
            print("No locally authorized workspaces.")


def _snapshot(state: ClientState, binding_id: str) -> None:
    _snapshot_with_consent(state, binding_id)


def _upload(identity: dict[str, Any], binding: dict[str, Any], files: tuple[Any, ...]) -> dict[str, Any]:
    key = load_private_key(identity)
    device_id = str(identity["device_id"])
    project_id = str(binding["project_id"])
    binding_id = str(binding["binding_id"])
    entries = [item.manifest() for item in files]
    idempotency_key = secrets.token_urlsafe(24)
    begin_path = f"/api/v1/device/{device_id}/snapshot-uploads"
    begin_body = canonical_json({
        "project_id": project_id,
        "binding_id": binding_id,
        "idempotency_key": idempotency_key,
        "files": entries,
    })
    with api_client(str(identity["server_url"])) as client:
        begun = signed_http_request(client, identity, key, "POST", begin_path, begin_body)
        upload_id = begun.get("upload_id")
        chunk_bytes = begun.get("chunk_bytes")
        if not _opaque_id(upload_id) or type(chunk_bytes) is not int or not 1 <= chunk_bytes <= CHUNK_BYTES:
            raise ClientProtocolError("Core returned invalid snapshot upload parameters.")
        for file_index, item in enumerate(files):
            data = item.data
            for chunk_index, offset in enumerate(range(0, len(data), chunk_bytes)):
                block = data[offset:offset + chunk_bytes]
                path = (
                    f"/api/v1/device/{device_id}/snapshot-uploads/{upload_id}/files/"
                    f"{file_index}/chunks/{chunk_index}"
                )
                signed_http_request(client, identity, key, "PUT", path, block)
                print(f"\rUploading {file_index + 1}/{len(files)}: {min(offset + len(block), len(data))}/{len(data)} bytes", end="", flush=True)
        commit_path = f"/api/v1/device/{device_id}/snapshot-uploads/{upload_id}/commit"
        result = signed_http_request(client, identity, key, "POST", commit_path, b"")
        print()
    return result


def _disconnect(state: ClientState, forget: bool) -> None:
    identity = _identity(state)
    device_id = str(identity["device_id"])
    try:
        asyncio.run(_run_connection(state, once=True, disconnect=True))
    except ClientProtocolError:
        if not forget:
            raise
    if forget:
        for path in (state.identity_path, state.workspaces_path):
            if path.exists():
                path.unlink()
        print(f"Local credentials and workspace approvals removed for device {device_id}.")
        print("To invalidate server credentials, also revoke this device in the SynAI Devices page.")
    else:
        print("Disconnected. The locally stored device identity remains paired.")


def _diagnose(state: ClientState) -> None:
    config = _config(state)
    identity = state.read_identity() if state.identity_path.exists() else {}
    print(f"Client: synai-client {__version__}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Platform: Linux {platform.release()} ({platform.machine()})")
    print(f"Configured Core: {config['server_url']}")
    print(f"Device: {identity.get('device_id', 'not paired')}")
    print(f"Protocol: {identity.get('protocol_version', PROTOCOL_VERSION)}")
    print(f"Credential expiry: {identity.get('credential_expires_at', 'not paired')}")
    print(f"State directory: {state.home} (mode 0700 required)")
    print("No secret, private key, workspace path, or project content is displayed.")


async def _run_connection(state: ClientState, once: bool, disconnect: bool = False) -> None:
    identity = _identity(state)
    key = load_private_key(identity)
    server_url = str(identity["server_url"])
    backoff = 1.0
    while True:
        try:
            await _connection_once(state, identity, key, server_url, disconnect, once)
            if once or disconnect:
                return
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except (ClientProtocolError, WebSocketException, OSError, TimeoutError) as exc:
            if once:
                raise ClientProtocolError(str(exc)) from exc
            if "revoked" in str(exc).lower() or "credential" in str(exc).lower() and "invalid" in str(exc).lower():
                raise ClientProtocolError("Device credentials are expired or revoked; pair again after administrator review.") from exc
            print(f"Connection unavailable: {exc}. Retrying in {backoff:.0f}s.", file=sys.stderr)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _connection_once(
    state: ClientState,
    identity: dict[str, Any],
    key: Ed25519PrivateKey,
    server_url: str,
    disconnect: bool,
    once: bool,
) -> None:
    origin = server_origin(server_url)
    parts = urlsplit(origin)
    scheme = "wss" if parts.scheme == "https" else "ws"
    uri = f"{scheme}://{parts.netloc}{CONNECTION_PATH}"
    device_id = str(identity["device_id"])
    async with websockets.connect(
        uri,
        max_size=8192,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
    ) as socket:
        hello = sign_connection_message(key, "hello", device_id, {
            "credential": identity["credential"],
            "protocol_versions": [PROTOCOL_VERSION],
            "capabilities": CAPABILITIES,
            "agent_version": f"synai-client/{__version__}",
        }, 0)
        await socket.send(canonical_json(hello).decode("utf-8"))
        response = await asyncio.wait_for(socket.recv(), timeout=10)
        ack = _check_server_message(response, "hello_ack", device_id, 0)
        ack_payload = ack["payload"]
        if (
            ack_payload.get("server_identity") != origin
            or type(ack_payload.get("server_time")) is not int
            or ack_payload.get("heartbeat_seconds") != 15
            or ack_payload.get("capabilities") != [
                "heartbeat-v1", "device-status-v1", "snapshot-v1",
            ]
        ):
            raise ClientProtocolError("Connected Core identity does not match the configured server URL.")
        print(f"Connected to {origin}; protocol v{PROTOCOL_VERSION}.")
        if disconnect:
            frame = sign_connection_message(key, "disconnect", device_id, {}, 1)
            await socket.send(canonical_json(frame).decode("utf-8"))
            response = await asyncio.wait_for(socket.recv(), timeout=5)
            _check_server_message(response, "disconnect_ack", device_id, 1)
            return
        if once:
            return

        inbound: asyncio.Queue[object] = asyncio.Queue(maxsize=16)
        reader = asyncio.create_task(_read_server_messages(socket, inbound))
        sequence = 1
        send_lock = asyncio.Lock()

        async def send_client_message(message_type: str, payload: dict[str, Any]) -> int:
            nonlocal sequence
            async with send_lock:
                current_sequence = sequence
                frame = sign_connection_message(key, message_type, device_id, payload, current_sequence)
                await socket.send(canonical_json(frame).decode("utf-8"))
                sequence += 1
                return current_sequence

        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(15)
                await send_client_message("heartbeat", {})

        heartbeat = asyncio.create_task(heartbeat_loop())
        server_sequence = 0
        try:
            while True:
                raw = await inbound.get()
                if isinstance(raw, BaseException):
                    raise ClientProtocolError("Core connection was interrupted.") from raw
                message = _decode_server_message(raw, device_id, server_sequence + 1)
                server_sequence = int(message["sequence"])
                message_type = str(message["type"])
                payload = message["payload"]
                if message_type == "snapshot_request":
                    request = _validate_snapshot_request(payload, state, int(time.time()))
                    if request is None:
                        continue
                    progress_payload = {
                        "operation_id": request["operation_id"],
                        "project_id": request["project_id"],
                        "binding_id": request["binding_id"],
                        "status": "awaiting_consent",
                    }
                    await send_client_message("snapshot_progress", progress_payload)
                    loop = asyncio.get_running_loop()
                    try:
                        outcome = await asyncio.to_thread(
                            _snapshot_with_consent,
                            state,
                            str(request["binding_id"]),
                            int(request["expires_at"]),
                            lambda: asyncio.run_coroutine_threadsafe(
                                send_client_message("snapshot_progress", {
                                    **progress_payload,
                                    "status": "uploading",
                                }),
                                loop,
                            ).result(timeout=5),
                        )
                        terminal = "snapshot_completed" if outcome is not None else "snapshot_failed"
                        status = "completed" if outcome is not None else "declined"
                    except (ClientProtocolError, ClientStateError, WorkspaceError, OSError, ValueError) as exc:
                        print(f"Requested snapshot failed: {exc}", file=sys.stderr)
                        terminal = "snapshot_failed"
                        status = "failed"
                    await send_client_message(terminal, {
                        "operation_id": request["operation_id"],
                        "project_id": request["project_id"],
                        "binding_id": request["binding_id"],
                        "status": status,
                    })
                elif message_type == "heartbeat_ack":
                    if set(payload) != {"server_time"} or type(payload["server_time"]) is not int:
                        raise ClientProtocolError("Core sent an invalid heartbeat response.")
                elif message_type == "message_ack":
                    if set(payload) != {"operation_id"} or not _opaque_id(payload["operation_id"]):
                        raise ClientProtocolError("Core sent an invalid snapshot operation acknowledgment.")
                else:
                    raise ClientProtocolError("Core sent an unsupported operation; closing connection.")
        finally:
            heartbeat.cancel()
            reader.cancel()
            await asyncio.gather(heartbeat, reader, return_exceptions=True)


async def _read_server_messages(socket: Any, inbound: asyncio.Queue[object]) -> None:
    try:
        while True:
            message = await socket.recv()
            if inbound.full():
                raise ClientProtocolError("Core exceeded the bounded client message queue.")
            inbound.put_nowait(message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not inbound.full():
            inbound.put_nowait(exc)


def _check_server_message(
    raw: object,
    expected_type: str,
    device_id: str,
    expected_sequence: int,
) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
        raise ClientProtocolError("Core sent an invalid or oversized protocol message.")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClientProtocolError("Core sent malformed JSON.") from exc
    if (
        not isinstance(message, dict)
        or type(message.get("schema_version")) is not int
        or message.get("schema_version") != 1
        or message.get("type") != expected_type
        or message.get("device_id") != device_id
        or type(message.get("sequence")) is not int
        or message.get("sequence") != expected_sequence
        or not isinstance(message.get("payload"), dict)
    ):
        raise ClientProtocolError("Core sent an unsupported or mismatched protocol message.")
    if expected_type == "hello_ack" and message["payload"].get("protocol_version") != PROTOCOL_VERSION:
        raise ClientProtocolError("Core selected an unsupported device protocol version.")
    if expected_type == "hello_ack" and set(message["payload"]) != {
        "protocol_version", "server_identity", "server_time", "heartbeat_seconds", "capabilities",
    }:
        raise ClientProtocolError("Core sent an unsupported hello response.")
    return message


def _decode_server_message(raw: object, device_id: str, expected_sequence: int) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
        raise ClientProtocolError("Core sent an invalid or oversized protocol message.")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClientProtocolError("Core sent malformed JSON.") from exc
    if (
        not isinstance(message, dict)
        or set(message) != {"schema_version", "type", "device_id", "sequence", "payload"}
        or type(message.get("schema_version")) is not int
        or message["schema_version"] != 1
        or message.get("device_id") != device_id
        or type(message.get("sequence")) is not int
        or message["sequence"] != expected_sequence
        or not isinstance(message.get("type"), str)
        or not isinstance(message.get("payload"), dict)
    ):
        raise ClientProtocolError("Core sent a version, identity, or sequence mismatch.")
    return message


def _validate_snapshot_request(
    payload: dict[str, Any],
    state: ClientState,
    now: int,
) -> dict[str, Any] | None:
    if set(payload) != {"operation_id", "project_id", "binding_id", "expires_at", "capability"}:
        raise ClientProtocolError("Core sent an invalid snapshot request.")
    if (
        not _opaque_id(payload["operation_id"])
        or not _opaque_id(payload["project_id"])
        or not _opaque_id(payload["binding_id"])
        or type(payload["expires_at"]) is not int
        or payload["expires_at"] <= now
        or payload["expires_at"] > now + 10 * 60
        or payload["capability"] != "snapshot-v1"
    ):
        raise ClientProtocolError("Core snapshot request is expired or unsupported.")
    record = WorkspaceRegistry(state).find(str(payload["binding_id"]))
    if record["project_id"] != payload["project_id"]:
        raise ClientProtocolError("Core snapshot request does not match the locally authorized project binding.")
    return payload


def _snapshot_with_consent(
    state: ClientState,
    binding_id: str,
    expires_at: int | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    identity = _identity(state)
    binding = WorkspaceRegistry(state).find(binding_id)
    _verify_server_binding(identity, binding)
    preview = capture_workspace(binding)
    print(f"Project: {binding['project_id']}")
    print(f"Workspace alias: {binding['alias']}")
    print(f"Destination Core: {identity['server_url']} (TLS certificate validation enabled for remote hosts)")
    print("Privacy: source files are transmitted to SynAI Core and retained as an immutable snapshot.")
    print(f"Included: {len(preview.files)} file(s), {preview.total_bytes} byte(s)")
    for item in preview.files:
        print(f"  + {item.path} ({len(item.data)} bytes)")
    for path, reason in preview.excluded:
        print(f"  - {path}: {reason}")
    for path, reason in preview.rejected:
        print(f"  ! {path}: {reason}")
    if preview.rejected:
        raise ClientProtocolError("Preview contains rejected files or exceeds snapshot limits; no content was uploaded.")
    if not preview.files:
        raise ClientProtocolError("Preview contains no supported files; no content was uploaded.")
    print("Filtering is conservative, not a guarantee that secrets are absent.")
    try:
        approval = input("Type UPLOAD to approve this exact preview: ").strip()
    except EOFError:
        approval = ""
    if approval != "UPLOAD":
        print("Upload denied; no source content was transmitted.")
        return None
    if expires_at is not None and expires_at <= int(time.time()):
        raise ClientProtocolError("Snapshot request expired during local review; no content was uploaded.")
    _verify_server_binding(identity, binding)
    if expires_at is not None and expires_at <= int(time.time()):
        raise ClientProtocolError("Snapshot request expired before upload; no content was uploaded.")
    if progress_callback is not None:
        progress_callback()
    result = _upload(identity, binding, preview.files)
    manifest = result.get("manifest")
    if not isinstance(manifest, dict):
        raise ClientProtocolError("Core did not return a committed snapshot manifest.")
    digest = hashlib.sha256(canonical_json(manifest)).hexdigest()
    if digest != result.get("manifest_digest"):
        raise ClientProtocolError("Core-returned manifest digest did not verify.")
    if (
        result.get("project_id") != binding["project_id"]
        or result.get("workspace_binding_id") != binding_id
        or result.get("source_device_id") != identity["device_id"]
    ):
        raise ClientProtocolError("Core returned a snapshot for a different device, project, or binding.")
    print(f"Snapshot committed: {result['snapshot_id']} ({len(preview.files)} files; sha256 {digest})")
    return result


def _config(state: ClientState) -> dict[str, Any]:
    config = state.read_config()
    if config.get("schema_version") != 1 or not isinstance(config.get("server_url"), str):
        raise ClientStateError("Client configuration is invalid; run synai-client setup.")
    server_origin(config["server_url"])
    return config


def _verify_server_binding(identity: dict[str, Any], binding: dict[str, Any]) -> None:
    device_id = str(identity["device_id"])
    binding_id = str(binding["binding_id"])
    path = f"/api/v1/device/{device_id}/workspace-bindings/{binding_id}"
    key = load_private_key(identity)
    with api_client(str(identity["server_url"])) as client:
        result = signed_http_request(client, identity, key, "GET", path)
    if (
        result.get("id") != binding_id
        or result.get("project_id") != binding.get("project_id")
        or result.get("device_id") != device_id
        or result.get("status") != "active"
    ):
        raise ClientProtocolError("Core binding identity does not match this local workspace approval.")


def _identity(state: ClientState) -> dict[str, Any]:
    identity = state.read_identity()
    if identity.get("schema_version") != 1 or not _opaque_id(identity.get("device_id")):
        raise ClientStateError("Device identity is missing or invalid; run synai-client pair.")
    config = _config(state)
    if identity.get("server_url") != config["server_url"]:
        raise ClientStateError("Device identity is paired to a different Core URL.")
    if not isinstance(identity.get("credential"), str):
        raise ClientStateError("Device credential is missing; pair this client again.")
    expiry = identity.get("credential_expires_at")
    if type(expiry) is not int or expiry <= int(time.time()):
        raise ClientStateError("Device credential has expired; ask an administrator to re-pair the device.")
    return identity


def _http_error(response: httpx.Response) -> ClientProtocolError:
    try:
        message = response.json().get("error", {}).get("message", "Core rejected the request.")
    except (ValueError, AttributeError):
        message = "Core returned an invalid error response."
    return ClientProtocolError(f"Core request failed ({response.status_code}): {message}")


def _opaque_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


if __name__ == "__main__":
    main()
