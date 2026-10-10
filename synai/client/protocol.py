from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from synai.client.state import ClientStateError


PROTOCOL_VERSION = 1
CONNECTION_PATH = "/api/v1/client/v1/connect"
MAX_RESPONSE_BYTES = 1024 * 1024


class ClientProtocolError(Exception):
    pass


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def enrollment_message(
    challenge_id: str,
    challenge_secret: str,
    public_key: bytes,
    protocol_version: int,
    capabilities: dict[str, object],
) -> bytes:
    return b"synai-device-pairing-v1\n" + canonical_json({
        "challenge_id": challenge_id,
        "challenge_secret": challenge_secret,
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "protocol_version": protocol_version,
        "capabilities": capabilities,
    })


def signable_message(
    message_type: str,
    device_id: str,
    timestamp: int,
    nonce: str,
    payload: dict[str, Any],
    sequence: int,
) -> bytes:
    return canonical_json({
        "schema_version": 1,
        "type": message_type,
        "device_id": device_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "sequence": sequence,
        "payload": payload,
    })


def http_request_message(method: str, path: str, timestamp: int, nonce: str, body: bytes) -> bytes:
    if not method or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("Device request method or path is invalid.")
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"synai-device-request-v1\n{method.upper()}\n{quote(path, safe='/:')}\n"
        f"{timestamp}\n{nonce}\n{digest}"
    ).encode("ascii")


def server_origin(server_url: str) -> str:
    parts = urlsplit(server_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ClientProtocolError("Core URL must be an HTTP(S) origin without embedded credentials.")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ClientProtocolError("Core URL must not include a path, query, or fragment.")
    import ipaddress

    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = parts.hostname.lower() == "localhost"
    if not loopback and parts.scheme != "https":
        raise ClientProtocolError("Non-loopback Core connections require HTTPS/WSS with validated TLS.")
    return f"{parts.scheme}://{parts.netloc}"


def api_client(server_url: str) -> httpx.Client:
    origin = server_origin(server_url)
    return httpx.Client(
        base_url=origin,
        timeout=httpx.Timeout(20, connect=10),
        verify=True,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )


def load_private_key(identity: dict[str, Any]) -> Ed25519PrivateKey:
    encoded = identity.get("private_key_pem")
    if not isinstance(encoded, str) or len(encoded) > 4096:
        raise ClientStateError("Device private key is missing or invalid.")
    try:
        key = serialization.load_pem_private_key(encoded.encode("ascii"), password=None)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ClientStateError("Device private key is invalid.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ClientStateError("Stored device key is not Ed25519.")
    return key


def signed_http_request(
    client: httpx.Client,
    identity: dict[str, Any],
    key: Ed25519PrivateKey,
    method: str,
    path: str,
    body: bytes = b"",
) -> dict[str, Any]:
    device_id = identity.get("device_id")
    credential = identity.get("credential")
    if not isinstance(device_id, str) or not isinstance(credential, str):
        raise ClientStateError("Device pairing credentials are incomplete.")
    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(24)
    signature = base64.b64encode(key.sign(
        http_request_message(method, path, timestamp, nonce, body),
    )).decode("ascii")
    headers = {
        "X-SynAI-Device-ID": device_id,
        "X-SynAI-Device-Credential": credential,
        "X-SynAI-Device-Timestamp": str(timestamp),
        "X-SynAI-Device-Nonce": nonce,
        "X-SynAI-Device-Signature": signature,
        "Accept": "application/json",
    }
    if body:
        headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(3):
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(24)
        headers["X-SynAI-Device-Timestamp"] = str(timestamp)
        headers["X-SynAI-Device-Nonce"] = nonce
        headers["X-SynAI-Device-Signature"] = base64.b64encode(key.sign(
            http_request_message(method, path, timestamp, nonce, body),
        )).decode("ascii")
        try:
            response = client.request(method, path, content=body, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise ClientProtocolError("Core connection failed; no operation was confirmed.") from exc
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ClientProtocolError("Core response exceeded the client safety limit.")
        if not response.is_success:
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message", "Core rejected the device request.")
            except (ValueError, AttributeError):
                detail = "Core returned an invalid error response."
            raise ClientProtocolError(f"Core request failed ({response.status_code}): {detail}")
        if response.status_code == 204:
            return {}
        try:
            result = response.json()
        except ValueError as exc:
            raise ClientProtocolError("Core returned invalid JSON.") from exc
        if not isinstance(result, dict):
            raise ClientProtocolError("Core returned an unexpected response.")
        return result
    raise ClientProtocolError("Core connection failed.") from last_error


def sign_connection_message(
    key: Ed25519PrivateKey,
    message_type: str,
    device_id: str,
    payload: dict[str, Any],
    sequence: int,
) -> dict[str, Any]:
    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(24)
    message = {
        "schema_version": 1,
        "type": message_type,
        "device_id": device_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "sequence": sequence,
        "payload": payload,
    }
    signature = key.sign(http_request_message(
        "GET", CONNECTION_PATH, timestamp, nonce, canonical_json(message),
    ))
    message["signature"] = base64.b64encode(signature).decode("ascii")
    return message
