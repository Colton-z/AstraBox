"""Built-in ``generic_json`` channel provider — the reference implementation.

The smallest REAL channel: an IM-agnostic JSON callback shape any platform
bridge (or curl) can speak, doubling as the reference for third-party
channel providers (see :mod:`astrabox.seams.channel` for the seam contract).

Inbound (``scene = "channel:generic_json"``, POSTed to the normal webhook
trigger endpoint):

* auth — the binding's secret as a bearer-style token
  (``X-Channel-Secret`` header, or ``Authorization: Bearer <secret>``),
  compared constant-time; 401 on mismatch. REPLAY BOUNDARY: a static bearer
  is inherently replayable — a captured secret authorizes any request until
  the binding is rotated, and this reference provider adds no timestamp or
  body binding. It is adequate for a trusted internal caller over TLS; a
  public-internet or untrusted-network integration MUST use a provider with a
  signed-timestamp + body-bound scheme (Slack/DingTalk/Feishu-style — see the
  built-in ``hmac`` webhook scene for the pattern), NOT this one.
* payload — ``{"text": "...", "message_id": "...", "conversation_id": "...",
  "sender": "...", "reply_to": "...", "action": "...", "reply": {...}}``;
  ``text`` (required unless ``action`` is set) becomes the turn message.
  ``message_id`` (optional) rides as the at-least-once dedup key — platform
  redeliveries of the same id get an idempotent ``duplicate`` ack.
  ``conversation_id`` (optional) rides as the conversation key — messages
  sharing it land in the SAME agent session instead of one session per
  message. ``sender`` (optional) is the sending account as the bridge names
  it, matched verbatim against the binding's ``allowed_participants``; a
  binding with that list refuses a message without it.
  ``reply_to`` (optional) is the ``message_id`` of a delivered reply this
  message answers: a reply to a question's message answers that question.
  ``action`` (optional) is a ``question.choices[].action`` the user picked.
  ``reply.callback_url`` (optional) enables outbound delivery.

Outbound: ``POST {"text": "<assistant reply>", "message_id": "<id>"}`` to
``reply.callback_url``. When the agent is waiting on a question, ``text``
already contains it and ``question`` carries it structured (choices with their
``action`` ids) for a bridge that renders its own buttons. The URL is
validated http(s) at inbound time (fail-loud), so a bad binding surfaces on
the trigger, not silently in the background task.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import uuid
from typing import Any, Mapping

import httpx

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.seams.channel import (
    ChannelDeliveryReceipt,
    ChannelDescriptor,
    ChannelInbound,
    ChannelInteraction,
    ChannelProvider,
    register_channel,
)

logger = get_logger(__name__)

_OUTBOUND_TIMEOUT_SECONDS = 15.0


def _strip_bearer(value: str) -> str:
    text = str(value or "").strip()
    return text[7:].strip() if text.lower().startswith("bearer ") else text


class GenericJsonChannelProvider(ChannelProvider):
    name = "generic_json"
    supports_interaction_actions = True

    def describe(self) -> ChannelDescriptor:
        return ChannelDescriptor(name=self.name, label="Generic JSON webhook")

    def verify_and_resolve(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        binding: Mapping[str, Any],
    ) -> ChannelInbound:
        secret = str(binding.get("secret") or "")
        provided = str(
            headers.get("x-channel-secret")
            or _strip_bearer(headers.get("authorization") or "")
        )
        if not secret or not provided or not hmac.compare_digest(secret, provided):
            raise APIError(
                code="CHANNEL_UNAUTHORIZED",
                message="invalid channel secret",
                status_code=401,
            )

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise APIError(
                code="CHANNEL_BAD_PAYLOAD",
                message=f"body must be JSON: {exc}",
                status_code=400,
            ) from exc
        if not isinstance(payload, dict):
            raise APIError(
                code="CHANNEL_BAD_PAYLOAD",
                message="body must be a JSON object",
                status_code=400,
            )
        text = str(payload.get("text") or "").strip()
        action = str(payload.get("action") or "").strip() or None
        if not text and action is None:
            raise APIError(
                code="CHANNEL_BAD_PAYLOAD",
                message='payload.text is required (the turn message)',
                status_code=400,
            )

        reply_context: dict[str, Any] | None = None
        reply = payload.get("reply")
        if isinstance(reply, dict):
            callback_url = str(reply.get("callback_url") or "").strip()
            if callback_url:
                if not callback_url.lower().startswith(("http://", "https://")):
                    raise APIError(
                        code="CHANNEL_BAD_PAYLOAD",
                        message="reply.callback_url must be http(s)",
                        status_code=400,
                    )
                reply_context = {"callback_url": callback_url}
        return ChannelInbound(
            content=text,
            reply_context=reply_context,
            dedup_key=str(payload.get("message_id") or "").strip() or None,
            conversation_key=str(payload.get("conversation_id") or "").strip() or None,
            participant=str(payload.get("sender") or "").strip() or None,
            reference=str(payload.get("reply_to") or "").strip() or None,
            action=action,
        )

    async def deliver_outbound(
        self,
        *,
        reply_context: dict[str, Any],
        text: str,
        binding: Mapping[str, Any],
        interaction: ChannelInteraction | None = None,
    ) -> ChannelDeliveryReceipt | None:
        _ = binding
        callback_url = str(reply_context.get("callback_url") or "").strip()
        if not callback_url:
            return None
        # The id a later ``reply_to`` names; minted here because the callback
        # receiver has no message id of its own to give back.
        message_id = f"astrabox-{uuid.uuid4().hex}"
        body: dict[str, Any] = {"text": text, "message_id": message_id}
        if interaction is not None:
            body["question"] = dataclasses.asdict(interaction)
        async with httpx.AsyncClient(timeout=_OUTBOUND_TIMEOUT_SECONDS) as client:
            response = await client.post(callback_url, json=body)
            response.raise_for_status()
        logger.info(
            "generic_json channel delivered reply (%d chars) to callback", len(text)
        )
        return ChannelDeliveryReceipt(message_ids=[message_id])


register_channel(GenericJsonChannelProvider())
