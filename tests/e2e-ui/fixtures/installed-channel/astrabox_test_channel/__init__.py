"""A separately installed messaging provider using only the public channel seam."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from typing import Any

import httpx

from astrabox.common.utils.errors import APIError
from astrabox.seams.channel import (
    SIGNAL_MESSAGE,
    ChannelAttention,
    ChannelDeliveryReceipt,
    ChannelDescriptor,
    ChannelDeliveryHandle,
    ChannelEvent,
    ChannelInbound,
    ChannelProvider,
    ChannelRecall,
)


class InstalledChannel(ChannelProvider):
    name = "installed_probe"

    def describe(self) -> ChannelDescriptor:
        return ChannelDescriptor(name=self.name, label="Installed package channel")

    def verify_and_resolve(
        self, *, headers: Mapping[str, str], raw_body: bytes, binding: Mapping[str, Any],
    ) -> ChannelInbound:
        secret = str(binding.get("secret") or "")
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(headers.get("x-installed-signature", ""), expected):
            raise APIError(code="CHANNEL_UNAUTHORIZED", message="invalid package signature", status_code=401)
        payload = json.loads(raw_body)
        callback_url = payload.get("callback_url")
        recall = payload.get("recall")
        return ChannelInbound(
            content=str(payload.get("utterance") or ""), dedup_key=payload["event_id"],
            conversation_key=payload.get("room_id"),
            reply_context={"callback_url": callback_url} if callback_url else None,
            participant=payload.get("sender"),
            message_timestamp=payload.get("sent_at"),
            retain_context=bool(payload.get("retain")),
            attention=ChannelAttention(is_mention=True) if payload.get("mention") else None,
            recall=(
                ChannelRecall(message_id=recall["message_id"], timestamp=recall["timestamp"])
                if recall else None
            ),
            signal=str(payload.get("signal") or SIGNAL_MESSAGE),
        )

    async def deliver_outbound(
        self, *, reply_context: dict[str, Any], text: str, binding: Mapping[str, Any],
    ) -> ChannelDeliveryReceipt:
        message_id = f"installed-{uuid.uuid4().hex}"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                reply_context["callback_url"],
                json={"text": "[installed-package] " + text, "message_id": message_id},
            )
            response.raise_for_status()
        return ChannelDeliveryReceipt(message_ids=[message_id])


class _StreamingDelivery(ChannelDeliveryHandle):
    def __init__(self, context: dict[str, Any], aliases: list[str]) -> None:
        self._url = context["callback_url"]
        self._alias = aliases[0] if aliases else f"installed-{uuid.uuid4().hex}"

    async def emit(self, event: ChannelEvent) -> ChannelDeliveryReceipt:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(self._url, json={
                "type": event.type, "text": event.text, "error": event.error,
                "message_id": self._alias, "response_id": event.response_id,
            })
            response.raise_for_status()
        return ChannelDeliveryReceipt(message_ids=[self._alias])


class InstalledStreamingChannel(InstalledChannel):
    """A separately installed provider exercising the public streaming contract."""

    name = "installed_stream_probe"
    supports_streaming_delivery = True

    async def open_delivery(self, *, reply_context, prior_aliases, binding) -> ChannelDeliveryHandle:
        return _StreamingDelivery(reply_context, prior_aliases)
