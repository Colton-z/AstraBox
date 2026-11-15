"""Built-in ``generic_json`` channel provider — the reference implementation.

The smallest REAL channel: an IM-agnostic JSON callback shape any platform
bridge (or curl) can speak, doubling as the reference for third-party
channel providers (see :mod:`astrabox.seams.channel` for the seam contract).

Inbound (``scene = "channel:generic_json"``, POSTed to the normal webhook
trigger endpoint):

* auth — one of two, with the binding's secret. A signed request
  (``X-Webhook-Timestamp`` and ``X-Webhook-Signature``, the scheme of
  :mod:`astrabox.common.utils.webhook_signature`) binds the timestamp and the
  body, so a captured request cannot carry another body and expires with the
  freshness window. The secret as a bearer-style token (``X-Channel-Secret``,
  or ``Authorization: Bearer <secret>``) is replayable for as long as the
  binding keeps its secret, and is adequate only for a trusted internal
  caller over TLS. A binding whose ``signed_requests`` setting is on accepts
  signed requests only. 401 on mismatch.
* payload — ``{"text": "...", "data": {...}, "message_id": "...",
  "conversation_id": "...", "sender": "...", "reply_to": "...", "action": "...",
  "reply": {...}}``;
  ``text`` becomes the turn message. ``data`` (optional, any JSON value)
  carries an event's fields: it follows ``text`` as JSON after a blank line,
  or is the whole message when there is no ``text``. One of ``text``,
  ``data`` or ``action`` is required.
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
from astrabox.common.utils.webhook_signature import signed_request_refusal
from astrabox.seams.channel import (
    ChannelDeliveryReceipt,
    ChannelDescriptor,
    ChannelField,
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
        return ChannelDescriptor(
            name=self.name,
            label="Generic JSON webhook",
            participant_format="the request's sender value, exactly as sent",
            participant_example="user123",
            config_fields=(
                ChannelField(
                    key="signed_requests",
                    label="Require signed requests",
                    required=False,
                    kind="boolean",
                    default=False,
                    help=(
                        "Accept only requests signed with the secret "
                        "(X-Webhook-Timestamp and X-Webhook-Signature), not the "
                        "secret sent as a bearer token."
                    ),
                ),
            ),
        )

    def verify_and_resolve(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        binding: Mapping[str, Any],
    ) -> ChannelInbound:
        secret = str(binding.get("secret") or "")
        config = binding.get("channel_config")
        signed_only = bool((config if isinstance(config, Mapping) else {}).get("signed_requests"))
        if signed_only or headers.get("x-webhook-signature"):
            refusal = signed_request_refusal(secret, headers, raw_body)
            if refusal is not None:
                raise APIError(code="CHANNEL_UNAUTHORIZED", message=refusal, status_code=401)
        else:
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
        data = payload.get("data")
        if data is not None:
            # The event's fields reach the Agent as the JSON the sender wrote,
            # after the text and a blank line: the Webhook trigger's own
            # "prefix, blank line, payload" shape. Reading them is the Agent's
            # work; nothing here selects or renames a field.
            fields = json.dumps(data, ensure_ascii=False)
            text = f"{text}\n\n{fields}" if text else fields
        if not text and action is None:
            raise APIError(
                code="CHANNEL_BAD_PAYLOAD",
                message="payload.text or payload.data is required (the turn message)",
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
