"""Authenticated client for AstraBox's private channel adapter gateway."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

CHANNEL_GATEWAY_HOST = "127.0.0.1"
CHANNEL_GATEWAY_PORT = 8765
CHANNEL_GATEWAY_BASE_URL_ENV = "ASTRABOX_CHANNEL_GATEWAY_BASE_URL"
CHANNEL_GATEWAY_TOKEN_ENV = "ASTRABOX_CHANNEL_GATEWAY_TOKEN"
CHANNEL_GATEWAY_HOST_ENV = "ASTRABOX_CHANNEL_GATEWAY_HOST"
CHANNEL_GATEWAY_PORT_ENV = "ASTRABOX_CHANNEL_GATEWAY_PORT"
CHANNEL_GATEWAY_MANIFEST_ENV = "ASTRABOX_CHANNEL_GATEWAY_MANIFEST"
CHANNEL_GATEWAY_ORIGIN = f"http://{CHANNEL_GATEWAY_HOST}:{CHANNEL_GATEWAY_PORT}"

_GATEWAY_HEADER = "X-AstraBox-Gateway-Token"
_REQUEST_TIMEOUT_SECONDS = 30.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def channel_gateway_base_url(configured: str | None = None) -> str:
    """Return a safe gateway base URL; plaintext is loopback-only."""

    raw = str(
        configured
        if configured is not None
        else os.environ.get(CHANNEL_GATEWAY_BASE_URL_ENV) or CHANNEL_GATEWAY_ORIGIN
    ).strip()
    try:
        url = httpx.URL(raw)
    except Exception as exc:
        raise RuntimeError(
            f"{CHANNEL_GATEWAY_BASE_URL_ENV} is not a valid URL"
        ) from exc
    if url.scheme not in {"http", "https"} or not url.host:
        raise RuntimeError(
            f"{CHANNEL_GATEWAY_BASE_URL_ENV} must be an absolute HTTP(S) URL"
        )
    if url.username or url.password or url.query or url.fragment:
        raise RuntimeError(
            f"{CHANNEL_GATEWAY_BASE_URL_ENV} must not contain credentials, a query, "
            "or a fragment"
        )
    if url.scheme != "https" and str(url.host).lower() not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"{CHANNEL_GATEWAY_BASE_URL_ENV} must use HTTPS outside loopback"
        )
    return str(url).rstrip("/")


def channel_gateway_token(configured: str | None = None) -> str:
    token = str(
        configured
        if configured is not None
        else os.environ.get(CHANNEL_GATEWAY_TOKEN_ENV) or ""
    ).strip()
    if len(token) < 32:
        raise RuntimeError(
            f"{CHANNEL_GATEWAY_TOKEN_ENV} is missing or too short; start AstraBox "
            "through its onebox launcher or configure an external channel gateway"
        )
    return token


@dataclass(frozen=True)
class GatewayCallbackResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class ChannelGatewayError(RuntimeError):
    """A gateway request failed with an HTTP status or transport error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChannelGatewayClient:
    """Small fail-loud HTTP/WebSocket address contract for the gateway."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = channel_gateway_base_url(base_url)
        self.token = channel_gateway_token(token)
        self._transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {_GATEWAY_HEADER: self.token}

    def events_url(self, deployment_id: str) -> str:
        url = httpx.URL(
            f"{self.base_url}/v1/bindings/{str(deployment_id).strip()}/events"
        )
        scheme = "wss" if url.scheme == "https" else "ws"
        return str(url.copy_with(scheme=scheme))

    async def validate(
        self,
        provider: str,
        *,
        config: Mapping[str, Any],
        credentials: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = await self._post(
            f"/v1/providers/{provider}/validate",
            {"config": dict(config), "credentials": dict(credentials)},
        )
        normalized_config = payload.get("config")
        normalized_credentials = payload.get("credentials")
        if not isinstance(normalized_config, dict) or not isinstance(
            normalized_credentials, dict
        ):
            raise RuntimeError("channel gateway validation returned an invalid shape")
        return normalized_config, normalized_credentials

    async def deliver(
        self, deployment_id: str, payload: Mapping[str, Any]
    ) -> list[str]:
        result = await self._post(
            f"/v1/bindings/{deployment_id}/messages", dict(payload)
        )
        message_ids = result.get("message_ids")
        if not isinstance(message_ids, list) or not message_ids or not all(
            isinstance(value, str) and value for value in message_ids
        ):
            raise RuntimeError("channel gateway delivery returned no message receipts")
        return message_ids

    async def callback(
        self,
        deployment_id: str,
        *,
        method: str,
        path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> GatewayCallbackResponse:
        payload = await self._post(
            f"/v1/bindings/{deployment_id}/callback",
            {
                "method": method,
                "path": path,
                "query": query,
                "headers": dict(headers),
                "body_base64": base64.b64encode(body).decode("ascii"),
            },
        )
        status = payload.get("status")
        response_headers = payload.get("headers")
        encoded = payload.get("body_base64")
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 100 <= status <= 599
            or not isinstance(response_headers, dict)
            or not isinstance(encoded, str)
        ):
            raise RuntimeError("channel gateway callback returned an invalid response")
        try:
            response_body = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise RuntimeError(
                "channel gateway callback returned invalid base64"
            ) from exc
        return GatewayCallbackResponse(
            status_code=status,
            headers={str(key): str(value) for key, value in response_headers.items()},
            body=response_body,
        )

    async def health(self) -> None:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            response = await client.get(f"{self.base_url}/healthz")
            response.raise_for_status()

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self.base_url}{path}", headers=self.headers, json=dict(payload)
                )
        except httpx.HTTPError as exc:
            raise ChannelGatewayError(
                f"channel gateway transport failed: {type(exc).__name__}"
            ) from exc
        if response.is_error:
            detail = ""
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    detail = str(parsed.get("error") or "").strip()
            except Exception:
                pass
            suffix = f": {detail}" if detail else ""
            raise ChannelGatewayError(
                f"channel gateway request failed ({response.status_code}){suffix}",
                status_code=response.status_code,
            )
        try:
            parsed = response.json()
        except Exception as exc:
            raise RuntimeError("channel gateway returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("channel gateway returned a non-object response")
        return parsed


__all__ = [
    "CHANNEL_GATEWAY_BASE_URL_ENV",
    "CHANNEL_GATEWAY_HOST",
    "CHANNEL_GATEWAY_HOST_ENV",
    "CHANNEL_GATEWAY_MANIFEST_ENV",
    "CHANNEL_GATEWAY_ORIGIN",
    "CHANNEL_GATEWAY_PORT",
    "CHANNEL_GATEWAY_PORT_ENV",
    "CHANNEL_GATEWAY_TOKEN_ENV",
    "ChannelGatewayClient",
    "ChannelGatewayError",
    "GatewayCallbackResponse",
    "channel_gateway_base_url",
    "channel_gateway_token",
]
