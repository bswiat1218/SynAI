from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx

from synai.broker.config import BrokerConfig
from synai.broker.protocol import BrokerError, TaskClaim, canonical_json


MAX_SNAPSHOT_RESPONSE_BYTES = 90 * 1024 * 1024


class CoreBrokerClient:
    """Loopback-only Core client with certificate verification and Broker client identity."""

    def __init__(self, config: BrokerConfig) -> None:
        self.base_url = f"https://127.0.0.1:{config.core_port}"
        self.client = httpx.AsyncClient(
            verify=str(config.core_ca_file),
            cert=(str(config.broker_cert_file), str(config.broker_key_file)),
            timeout=httpx.Timeout(10, connect=3),
            follow_redirects=False,
            trust_env=False,
        )

    async def validate_live_claim(self, claim: TaskClaim, signature: str) -> bool:
        response = await self._post(
            "/v1/claims/validate",
            {"claim": claim.to_dict(), "signature": signature},
        )
        value = response.json()
        if not isinstance(value, dict) or type(value.get("authorized")) is not bool:
            raise BrokerError("core_response_invalid", "Core authority returned an invalid claim status.", 502)
        return value["authorized"]

    async def mark_operation_running(self, claim: TaskClaim, signature: str) -> None:
        await self._post(
            "/v1/claims/running",
            {"claim": claim.to_dict(), "signature": signature},
        )

    async def record_operation_state(
        self,
        claim: TaskClaim,
        signature: str,
        state: str,
    ) -> None:
        await self._post(
            "/v1/claims/outcome",
            {"claim": claim.to_dict(), "signature": signature, "state": state},
        )

    async def fetch_exact(
        self,
        *,
        claim: TaskClaim,
        signature: str,
        grant_token: str,
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        response = await self._post(
            "/v1/snapshots/retrieve",
            {
                "claim": claim.to_dict(), "signature": signature,
                "grant_token": grant_token,
            },
            maximum=MAX_SNAPSHOT_RESPONSE_BYTES,
        )
        try:
            value = response.json()
            manifest = value["manifest"]
            raw_files = value["files"]
            if not isinstance(manifest, dict) or not isinstance(raw_files, dict):
                raise ValueError("Core snapshot payload shape is invalid")
            files = {
                str(path): base64.b64decode(encoded, validate=True)
                for path, encoded in raw_files.items()
                if isinstance(path, str) and isinstance(encoded, str)
            }
            if len(files) != len(raw_files):
                raise ValueError("Core snapshot file encoding is invalid")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BrokerError("core_snapshot_invalid", "Core returned an invalid authorized snapshot.", 502) from exc
        if (
            manifest.get("project_id") != claim.project_id
            or manifest.get("snapshot_id") != claim.snapshot_id
            or hashlib.sha256(canonical_json(manifest)).hexdigest() != claim.snapshot_digest
        ):
            raise BrokerError("core_snapshot_binding_mismatch", "Core snapshot response differs from its signed claim.", 502)
        return manifest, files

    async def record_result(
        self,
        claim: TaskClaim,
        signature: str,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._post(
            "/v1/results",
            {"claim": claim.to_dict(), "signature": signature, "envelope": envelope},
        )
        result = response.json()
        if not isinstance(result, dict) or result.get("validated") is not True:
            raise BrokerError("core_result_rejected", "Core did not validate the Broker result.", 409)
        return result

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        maximum: int = 2 * 1024 * 1024,
    ) -> httpx.Response:
        try:
            async with self.client.stream(
                "POST", f"{self.base_url}{path}", json=payload,
            ) as streamed:
                content = bytearray()
                async for chunk in streamed.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise BrokerError("core_response_too_large", "Core response exceeds its configured bound.", 413)
                response = httpx.Response(
                    streamed.status_code, headers=streamed.headers,
                    content=bytes(content), request=streamed.request,
                )
            response.raise_for_status()
            return response
        except BrokerError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
            raise BrokerError("core_authority_unavailable", "Core authority is unavailable over mTLS.", 503) from exc
        except httpx.HTTPStatusError as exc:
            code = "core_authority_rejected"
            try:
                error = exc.response.json().get("error", {})
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"][:64]
            except (ValueError, AttributeError):
                pass
            raise BrokerError(code, "Core authority rejected the Broker request.", exc.response.status_code) from exc
