"""Chat providers backed by AstraBox's bundled official Satori adapters.

The public seam normally exposes one provider per platform (``channel:telegram``,
``channel:discord``, ...). ``channel:satori`` deliberately bridges an existing
Satori Protocol endpoint. AstraBox still owns credentials, Agent binding,
lifecycle, durable ingress, and delivery receipts for every provider.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from datetime import timezone
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Mapping

from satori import ChannelType, Event
from satori.element import (
    At,
    Author,
    Br,
    Element,
    Emoji,
    Link,
    Paragraph,
    Quote,
    Resource,
    Sharp,
    Text,
)
from websockets.asyncio.client import connect as websocket_connect

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.providers.channel_gateway import ChannelGatewayClient, ChannelGatewayError
from astrabox.seams.channel import (
    ChannelAttention,
    ChannelCallbackResponse,
    ChannelDeliveryReceipt,
    ChannelDescriptor,
    ChannelField,
    ChannelInbound,
    ChannelProvider,
    ChannelRecall,
    ChannelSourceEnvelope,
    register_channel,
)

logger = get_logger(__name__)

_OP_EVENT = 0
_OP_PING = 1
_OP_PONG = 2
_OP_IDENTIFY = 3
_OP_READY = 4
_OP_STOP = 6
_OP_ACK = 7
_SOURCE_HANDSHAKE_TIMEOUT_SECONDS = 60.0
_SOURCE_HEARTBEAT_SECONDS = 10.0
_MAX_EVENT_BYTES = 2 * 1024 * 1024
_MESSAGE_CREATED = "message-created"
_MESSAGE_DELETED = "message-deleted"


def _manifest() -> tuple[dict[str, Any], ...]:
    path = Path(__file__).with_name("channel_gateway_manifest.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"could not read channel provider manifest {path}: {exc}") from exc
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(providers, list)
    ):
        raise RuntimeError("channel provider manifest must have version=1 and providers")
    names: set[str] = set()
    output: list[dict[str, Any]] = []
    for raw in providers:
        if not isinstance(raw, dict):
            raise RuntimeError("channel provider manifest entries must be objects")
        name = str(raw.get("name") or "").strip()
        if not name or name in names:
            raise RuntimeError(f"channel provider manifest has duplicate name {name!r}")
        names.add(name)
        output.append(dict(raw))
    return tuple(output)


def _field(raw: Mapping[str, Any], *, secret: bool) -> ChannelField:
    return ChannelField(
        key=str(raw.get("key") or ""),
        label=str(raw.get("label") or raw.get("key") or ""),
        required=bool(raw.get("required", True)),
        secret=secret,
        kind=str(raw.get("kind") or "string"),
        options=tuple(str(value) for value in raw.get("options") or ()),
        default=raw.get("default"),
        placeholder=str(raw.get("placeholder") or ""),
        help=str(raw.get("help") or ""),
    )


def _alias(platform: str, self_id: str, message_id: str) -> str:
    return f"{platform}:{self_id}:message:{message_id}"


def _event_dedup_key(payload: Mapping[str, Any], raw_body: bytes) -> str:
    login = payload.get("login")
    if not isinstance(login, Mapping):
        login = {}
    login_user = login.get("user")
    if not isinstance(login_user, Mapping):
        login_user = {}
    platform = str(login.get("platform") or payload.get("platform") or "unknown")
    self_id = str(login_user.get("id") or payload.get("self_id") or "unknown")
    event_type = str(payload.get("type") or "unknown")
    sequence = payload.get("sn")
    identity = (
        str(sequence)
        if isinstance(sequence, int) and not isinstance(sequence, bool)
        else hashlib.sha256(raw_body).hexdigest()
    )
    return f"{platform}:{self_id}:event:{event_type}:{identity}"


def _walk_unquoted(elements: Iterable[Element]) -> Iterable[Element]:
    for element in elements:
        if isinstance(element, Quote):
            continue
        yield element
        yield from _walk_unquoted(element.children)


def _render_elements(elements: Iterable[Element], *, self_id: str) -> str:
    parts: list[str] = []
    for element in elements:
        if isinstance(element, Quote):
            continue
        if isinstance(element, Text):
            parts.append(element.text)
            continue
        if isinstance(element, At):
            if element.id == self_id:
                continue
            target = element.name or element.id or element.role or element.type
            if target:
                parts.append(f"@{target}")
            continue
        if isinstance(element, Sharp):
            parts.append(f"#{element.name or element.id}")
            continue
        if isinstance(element, Emoji):
            parts.append(f":{element.name or element.id}:")
            continue
        if isinstance(element, Resource):
            title = element.title or element.tag
            parts.append(f"[{title}]({element.src})")
            continue
        if isinstance(element, Br):
            parts.append("\n")
            continue
        if isinstance(element, Link):
            label = _render_elements(element.children, self_id=self_id)
            parts.append(f"{label or element.href} ({element.href})")
            continue
        rendered_children = _render_elements(element.children, self_id=self_id)
        if isinstance(element, Paragraph) and rendered_children:
            parts.extend(("\n", rendered_children, "\n"))
        else:
            parts.append(rendered_children)
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


class _GatewaySourceEnvelope(ChannelSourceEnvelope):
    def __init__(
        self,
        *,
        websocket: Any,
        deployment_id: str,
        inbound: ChannelInbound,
        source_cursor: int,
    ) -> None:
        self._websocket = websocket
        self.deployment_id = deployment_id
        self.inbound = inbound
        self.source_cursor = source_cursor

    async def ack(self, receipt: Any) -> None:
        _ = receipt
        await self._websocket.send(
            json.dumps({"op": _OP_ACK, "body": {"cursor": self.source_cursor}})
        )

    async def nack(self, error: str) -> None:
        _ = error
        await self._websocket.close(code=1011, reason="durable channel ingest failed")


class GatewayChannelProvider(ChannelProvider):
    """One channel product routed through the private channel gateway."""

    supports_source = True
    uses_trigger_secret = False

    def __init__(
        self,
        definition: Mapping[str, Any],
        *,
        gateway: ChannelGatewayClient | None = None,
    ) -> None:
        self._definition = dict(definition)
        self.name = str(definition.get("name") or "").strip()
        self._gateway_override = gateway
        self._descriptor = ChannelDescriptor(
            name=self.name,
            label=str(definition.get("label") or self.name),
            config_fields=tuple(
                _field(value, secret=False)
                for value in definition.get("config_fields") or ()
            ),
            credential_fields=tuple(
                _field(value, secret=True)
                for value in definition.get("credential_fields") or ()
            ),
            callback_path=(
                str(definition.get("callback_path"))
                if definition.get("callback_path")
                else None
            ),
            setup_url=(
                str(definition.get("setup_url"))
                if definition.get("setup_url")
                else None
            ),
            documentation_url=(
                str(definition.get("documentation_url"))
                if definition.get("documentation_url")
                else None
            ),
        )

    @property
    def gateway(self) -> ChannelGatewayClient:
        return self._gateway_override or ChannelGatewayClient()

    def describe(self) -> ChannelDescriptor:
        return self._descriptor

    @staticmethod
    def _normalize_fields(
        fields: tuple[ChannelField, ...], value: Mapping[str, Any], *, label: str
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} must be an object")
        raw = dict(value)
        allowed = {field.key for field in fields}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"{label} has unsupported fields: "
                + ", ".join(sorted(str(key) for key in unknown))
            )
        normalized: dict[str, Any] = {}
        for field in fields:
            candidate = raw.get(field.key, field.default)
            missing = candidate is None or candidate == ""
            if missing:
                if field.required:
                    raise ValueError(f"{label}.{field.key} is required")
                continue
            if field.kind in {"string", "select"}:
                if not isinstance(candidate, str):
                    raise ValueError(f"{label}.{field.key} must be a string")
                candidate = candidate.strip()
            elif field.kind == "number":
                if (
                    not isinstance(candidate, (int, float))
                    or isinstance(candidate, bool)
                ):
                    raise ValueError(f"{label}.{field.key} must be a number")
            elif field.kind == "boolean" and not isinstance(candidate, bool):
                raise ValueError(f"{label}.{field.key} must be a boolean")
            if field.options and candidate not in field.options:
                raise ValueError(
                    f"{label}.{field.key} must be one of {list(field.options)}"
                )
            normalized[field.key] = candidate
        return normalized

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        return self._normalize_fields(
            self._descriptor.config_fields, config, label="channel_config"
        )

    def normalize_credentials(
        self, credentials: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._normalize_fields(
            self._descriptor.credential_fields,
            credentials,
            label="credentials",
        )

    async def validate_configuration(
        self,
        *,
        config: Mapping[str, Any],
        credentials: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            local_config = self.normalize_config(config)
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="CHANNEL_CONFIG_INVALID",
                message=str(exc),
                status_code=400,
            ) from exc
        try:
            local_credentials = self.normalize_credentials(credentials)
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="CHANNEL_CREDENTIALS_INVALID",
                message=str(exc),
                status_code=400,
            ) from exc
        try:
            return await self.gateway.validate(
                self.name,
                config=local_config,
                credentials=local_credentials,
            )
        except ChannelGatewayError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                raise APIError(
                    code="CHANNEL_CONFIG_INVALID",
                    message=str(exc),
                    status_code=400,
                ) from exc
            raise APIError(
                code="CHANNEL_GATEWAY_UNAVAILABLE",
                message=str(exc),
                status_code=503,
            ) from exc
        except Exception as exc:
            raise APIError(
                code="CHANNEL_GATEWAY_UNAVAILABLE",
                message=f"channel provider validation failed: {exc}",
                status_code=503,
            ) from exc

    def verify_and_resolve(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        binding: Mapping[str, Any],
    ) -> ChannelInbound:
        _ = (headers, raw_body, binding)
        raise APIError(
            code="CHANNEL_CALLBACK_PATH_REQUIRED",
            message=(
                f"channel {self.name!r} receives events through its managed source"
                + (
                    f" and {self._descriptor.callback_path} callback"
                    if self._descriptor.callback_path
                    else ""
                )
            ),
            status_code=404,
        )

    def open_source(self, *, binding: Mapping[str, Any]) -> Any:
        async def _events() -> Any:
            deployment_id = str(binding.get("deployment_id") or "").strip()
            if not deployment_id:
                raise RuntimeError("channel source binding requires deployment_id")
            config = binding.get("channel_config")
            credentials = binding.get("channel_credentials")
            if not isinstance(config, Mapping) or not isinstance(credentials, Mapping):
                raise RuntimeError("channel source binding is missing config or credentials")
            normalized_config = self.normalize_config(config)
            normalized_credentials = self.normalize_credentials(credentials)
            callback_base_url = str(binding.get("callback_base_url") or "").strip()
            if self._descriptor.callback_path and not callback_base_url:
                raise RuntimeError("channel callback binding has no public callback base URL")
            source_cursor = binding.get("source_cursor")
            if source_cursor is None:
                source_cursor = 0
            if (
                not isinstance(source_cursor, int)
                or isinstance(source_cursor, bool)
                or source_cursor < 0
            ):
                raise RuntimeError("channel source cursor must be a non-negative integer")

            gateway = self.gateway
            async with websocket_connect(
                gateway.events_url(deployment_id),
                additional_headers=gateway.headers,
                ping_interval=None,
                open_timeout=_SOURCE_HANDSHAKE_TIMEOUT_SECONDS,
                max_size=_MAX_EVENT_BYTES,
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "op": _OP_IDENTIFY,
                            "body": {
                                "provider": self.name,
                                "config": normalized_config,
                                "credentials": normalized_credentials,
                                "callback_base_url": callback_base_url,
                                "cursor": source_cursor,
                            },
                        },
                        separators=(",", ":"),
                    )
                )
                raw_ready = await asyncio.wait_for(
                    websocket.recv(), timeout=_SOURCE_HANDSHAKE_TIMEOUT_SECONDS
                )
                ready = self._decode_signal(raw_ready)
                if ready.get("op") != _OP_READY:
                    raise RuntimeError("channel gateway did not answer IDENTIFY with READY")

                heartbeat = asyncio.create_task(
                    self._heartbeat(websocket),
                    name=f"channel-gateway-heartbeat-{deployment_id}",
                )
                try:
                    async for raw_signal in websocket:
                        signal = self._decode_signal(raw_signal)
                        opcode = signal.get("op")
                        if opcode in (_OP_PONG, _OP_READY):
                            continue
                        if opcode != _OP_EVENT:
                            raise RuntimeError(
                                f"unexpected channel gateway opcode {opcode!r}"
                            )
                        payload = signal.get("body")
                        if not isinstance(payload, dict):
                            raise RuntimeError("channel gateway EVENT body must be an object")
                        sequence = payload.get("sn")
                        if not isinstance(sequence, int) or isinstance(sequence, bool):
                            raise RuntimeError("channel gateway EVENT requires an integer sn")
                        raw_body = json.dumps(
                            payload, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
                        yield _GatewaySourceEnvelope(
                            websocket=websocket,
                            deployment_id=deployment_id,
                            inbound=self._resolve_event(payload, raw_body=raw_body),
                            source_cursor=sequence,
                        )
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                    with contextlib.suppress(Exception):
                        await websocket.send(json.dumps({"op": _OP_STOP, "body": {}}))

        return _events()

    async def deliver_outbound(
        self,
        *,
        reply_context: dict[str, Any],
        text: str,
        binding: Mapping[str, Any],
    ) -> ChannelDeliveryReceipt:
        deployment_id = str(binding.get("deployment_id") or "").strip()
        platform = str(reply_context.get("platform") or "").strip()
        self_id = str(reply_context.get("self_id") or "").strip()
        channel_id = str(reply_context.get("channel_id") or "").strip()
        message_id = str(reply_context.get("message_id") or "").strip()
        if not deployment_id or not platform or not self_id or not channel_id:
            raise RuntimeError("channel delivery context is missing gateway routing")
        ids = await self.gateway.deliver(
            deployment_id,
            {
                "platform": platform,
                "self_id": self_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "text": text,
            },
        )
        return ChannelDeliveryReceipt(
            message_ids=[_alias(platform, self_id, message_id) for message_id in ids]
        )

    async def forward_callback(
        self,
        *,
        deployment_id: str,
        method: str,
        path: str,
        query: str,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> ChannelCallbackResponse:
        expected = str(self._descriptor.callback_path or "")
        if path != expected and not path.startswith(f"{expected}/"):
            raise APIError(
                code="CHANNEL_CALLBACK_NOT_FOUND",
                message="channel callback path does not match this provider",
                status_code=404,
            )
        try:
            result = await self.gateway.callback(
                deployment_id,
                method=method,
                path=path,
                query=query,
                headers=headers,
                body=raw_body,
            )
        except ChannelGatewayError as exc:
            raise APIError(
                code="CHANNEL_GATEWAY_UNAVAILABLE",
                message="channel connector is not ready",
                status_code=503,
            ) from exc
        return ChannelCallbackResponse(
            status_code=result.status_code,
            headers=result.headers,
            body=result.body,
        )

    def _resolve_event(
        self, payload: Mapping[str, Any], *, raw_body: bytes
    ) -> ChannelInbound:
        event_type = str(payload.get("type") or "")
        if event_type not in (_MESSAGE_CREATED, _MESSAGE_DELETED):
            return ChannelInbound(
                content="",
                dedup_key=_event_dedup_key(payload, raw_body),
                ignore_reason=(
                    f"channel event {str(payload.get('type') or 'unknown')!r} "
                    "is not message-created"
                ),
            )
        try:
            event = Event.parse(dict(payload))
        except Exception as exc:
            raise RuntimeError(f"invalid Satori message event: {exc}") from exc
        if event.message is None or event.channel is None or event.user is None:
            raise RuntimeError(
                f"{event_type} requires message, channel, and user resources"
            )
        platform = str(event.platform or "").strip()
        self_id = str(event.self_id or "").strip()
        message_id = str(event.message.id or "").strip()
        channel_id = str(event.channel.id or "").strip()
        participant_id = str(event.user.id or "").strip()
        if not all((platform, self_id, message_id, channel_id, participant_id)):
            raise RuntimeError(f"{event_type} contains an empty platform identity")
        timestamp = event.timestamp.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        if event_type == _MESSAGE_DELETED:
            source_event_key = payload.get("source_event_key")
            if not isinstance(source_event_key, str) or not source_event_key:
                raise RuntimeError("channel deletion lacks its original source event identity")
            return ChannelInbound(
                content="",
                dedup_key=f"{platform}:{self_id}:event:message-deleted:{source_event_key}",
                conversation_key=f"{platform}:{self_id}:channel:{channel_id}",
                participant=f"{platform}:{participant_id}",
                recall=ChannelRecall(
                    message_id=_alias(platform, self_id, message_id),
                    timestamp=timestamp,
                ),
            )
        try:
            elements = event.message.message
        except Exception as exc:
            raise RuntimeError(f"invalid Satori message content: {exc}") from exc
        content = _render_elements(elements, self_id=self_id)
        quotes = [element for element in elements if isinstance(element, Quote)]
        quote = quotes[0] if quotes else None
        reference_id = str((quote.id if quote is not None else None) or "").strip()
        mentions_bot = any(
            isinstance(element, At) and element.id == self_id
            for element in _walk_unquoted(elements)
        )
        replies_to_bot = bool(
            quote
            and any(
                isinstance(element, Author) and element.id == self_id
                for element in quote.children
            )
        )
        ignore_reason: str | None = None
        if participant_id == self_id:
            ignore_reason = "message was emitted by the connected bot account"
        elif event.user.is_bot is True:
            ignore_reason = "message was emitted by a bot account"
        elif not content:
            ignore_reason = "message contains no user-visible content"
        return ChannelInbound(
            content=content,
            reply_context={
                "platform": platform,
                "self_id": self_id,
                "channel_id": channel_id,
                "message_id": message_id,
            },
            dedup_key=_alias(platform, self_id, message_id),
            conversation_key=f"{platform}:{self_id}:channel:{channel_id}",
            attention=ChannelAttention(
                is_direct_message=event.channel.type == ChannelType.DIRECT,
                is_mention=mentions_bot,
                is_reply_to_agent=replies_to_bot,
            ),
            reference=(
                _alias(platform, self_id, reference_id) if reference_id else None
            ),
            participant=f"{platform}:{participant_id}",
            ignore_reason=ignore_reason,
            message_timestamp=timestamp,
            retain_context=event.channel.type != ChannelType.DIRECT,
        )

    @staticmethod
    async def _heartbeat(websocket: Any) -> None:
        while True:
            await asyncio.sleep(_SOURCE_HEARTBEAT_SECONDS)
            await websocket.send(json.dumps({"op": _OP_PING, "body": {}}))

    @staticmethod
    def _decode_signal(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            signal = json.loads(str(raw))
        except Exception as exc:
            raise RuntimeError("channel gateway sent invalid JSON") from exc
        if not isinstance(signal, dict):
            raise RuntimeError("channel gateway signal must be an object")
        return signal


CHANNEL_PROVIDER_DEFINITIONS = _manifest()
for _definition in CHANNEL_PROVIDER_DEFINITIONS:
    register_channel(GatewayChannelProvider(_definition))


__all__ = ["CHANNEL_PROVIDER_DEFINITIONS", "GatewayChannelProvider"]
