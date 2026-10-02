"""A separately installed messaging provider using only the public channel seam."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

import httpx

from astrabox.common.utils.errors import APIError
from astrabox.seams.channel import ChannelDescriptor, ChannelInbound, ChannelProvider


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
        return ChannelInbound(
            content=payload["utterance"], dedup_key=payload["event_id"],
            conversation_key=payload["room_id"],
            reply_context={"callback_url": payload["callback_url"]},
        )

    async def deliver_outbound(
        self, *, reply_context: dict[str, Any], text: str, binding: Mapping[str, Any],
    ) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                reply_context["callback_url"], json={"text": "[installed-package] " + text},
            )
            response.raise_for_status()
