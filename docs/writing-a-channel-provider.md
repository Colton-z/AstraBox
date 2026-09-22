# Add a messaging platform

A `ChannelProvider` connects a messaging product to an Agent Deployment. It
authenticates inbound events, maps message and conversation identities, and
delivers Agent replies through the product's official API.

When an installed adapter supports the product, configure it from the
Deployment page. Implement a `ChannelProvider` only for a new product or for a
different transport supported by that product's official API.

## Authentication and message identity

A messaging platform adapter has two independent responsibilities:

| Dimension | Problem it solves | AstraBox interface |
| --- | --- | --- |
| Channel authentication | Whether an inbound event came from the configured messaging product | `verify_and_resolve()` or an authenticated source connection |
| Message identity | Which messages are duplicates and which messages belong to the same conversation | `ChannelInbound.dedup_key` and `conversation_key` |

`dedup_key` is the messaging product's unique event or message ID. It prevents
a redelivered event from running twice. `conversation_key` is the stable chat,
room, ticket, or thread ID. It lets later messages continue the same Agent
conversation. They are not interchangeable.

## HTTP webhook mode

Use this mode when the messaging product supports signed HTTP callbacks:

1. **Package the adapter**: publish a Python distribution with a
   `ChannelProvider` entry point.
2. **Describe the setup fields**: declare non-secret configuration and
   write-only credentials for the AstraBox setup form.
3. **Authenticate and map the callback**: verify the product's signature over
   the raw request, then return a `ChannelInbound`.
4. **Deliver replies**: use the saved `reply_context` and freshly loaded
   credentials to send the Agent's response through the product API.
5. **Create a channel Deployment**: select the installed adapter and configure
   the messaging product to call the Deployment URL.

Configure the product to send events to:

```text
POST /api/v1/deployments/{deployment_id}/trigger
```

AstraBox saves the inbound work before acknowledging it. If the adapter returns
a stable `dedup_key`, repeated delivery resolves to the existing work instead
of starting another turn.

:::note

Some messaging products require their own callback path, methods, or response
body. For those products, declare `callback_path` and implement
`forward_callback()` instead of using the shared trigger response.

:::

## Message identity

Return a `ChannelInbound` with stable values from the messaging product:

| Field | Description |
| --- | --- |
| `content` | Text delivered to the Agent |
| `dedup_key` | Unique event or message ID that identifies redelivery |
| `conversation_key` | Stable conversation, room, ticket, or thread ID |
| `reply_context` | Non-secret routing values for delivering the reply |
| `attention` | Direct-message, mention, and reply signals |
| `participant` | External sender identity recorded with the message |

`reference` links an inbound reply to an earlier platform message.
`ack_extra` adds product-required fields to the callback response.
`alias_link` records a platform message ID that becomes known after delivery.
`ignore_reason` classifies an authenticated event that must not start a turn.

The Deployment's `attention_policy` decides whether an authenticated message
needs a direct-message, mention, or reply signal. The adapter reports those
facts; it does not decide the policy.

## Source connection mode

Use a source connection when the messaging product delivers messages through a
broker, long-lived connection, or official SDK rather than an HTTP callback.

Set `supports_source = True` and implement `open_source()`. Yield
`ChannelSourceEnvelope` values with stable `dedup_key` values. Acknowledge each
source message only after AstraBox returns the durable ingress receipt; reject
it when ingestion fails so the source can redeliver it.

When the source exposes an ordered replay cursor, include `source_cursor`.
AstraBox saves the cursor after durable ingestion and supplies it when the
connection is reopened.

`verify_and_resolve()` remains a required method. A source-only adapter must
reject the shared HTTP trigger explicitly, for example with a `404` `APIError`;
it must not accept an unauthenticated payload on a path it does not use.

## Configuration and credentials

The adapter describes setup fields with `ChannelDescriptor`:

| Field group | Stored as | Available to |
| --- | --- | --- |
| `config_fields` | Non-secret Deployment configuration | Callback verification, source connection, and outbound delivery |
| `credential_fields` | Encrypted, write-only secret values | Source connection and outbound delivery |

Credentials are accepted only when a Deployment is created or updated. Read
responses return `credentials_configured`, never the saved values.

Set `setup_url` to the messaging product's configuration page and
`documentation_url` to the adapter's setup guide. Override the asynchronous
`validate_configuration()` method when the product must verify credentials or
account settings remotely; a failed validation prevents that create or update
from being saved.

A direct HTTP callback is not hydrated with `credential_fields`. Verify it
with the Deployment trigger secret or with public verification material in
`config_fields`, following the messaging product's official webhook algorithm.
Use `credential_fields` for private tokens needed by a source connection or the
outbound product API.

## Package the adapter

Declare the adapter in the Python distribution:

```toml
[project]
name = "astrabox-channel-example"
requires-python = ">=3.11"

[project.entry-points."astrabox.providers.channel"]
example = "astrabox_channel_example:ExampleChannelProvider"
```

`ExampleChannelProvider.name` becomes the channel scene name
`channel:example`; use the same value for the entry-point name so package
discovery and product configuration stay aligned. AstraBox loads this group
when the service starts, validates the provider, and registers it. An
entry-point name collision or an incomplete capability fails startup. The
class may also set `seams_api_version`; an incompatible declared version fails
at startup instead of failing on the first message.

## Implement the adapter

Subclass `ChannelProvider` from `astrabox.seams.channel` and implement
`verify_and_resolve()`:

```python
from collections.abc import Mapping
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.seams.channel import (
    ChannelAttention,
    ChannelDescriptor,
    ChannelField,
    ChannelInbound,
    ChannelProvider,
)


class ExampleChannelProvider(ChannelProvider):
    name = "example"
    uses_trigger_secret = False

    def describe(self) -> ChannelDescriptor:
        return ChannelDescriptor(
            name=self.name,
            label="Example",
            config_fields=(
                ChannelField(
                    key="signing_public_key",
                    label="Signing public key",
                ),
            ),
            credential_fields=(
                ChannelField(
                    key="bot_token",
                    label="Bot token",
                    secret=True,
                ),
            ),
            documentation_url="https://example.com/webhook-docs",
        )

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        public_key = str(config.get("signing_public_key") or "").strip()
        if not public_key:
            raise ValueError("signing_public_key is required")
        return {"signing_public_key": public_key}

    def normalize_credentials(
        self, credentials: Mapping[str, Any]
    ) -> dict[str, Any]:
        token = str(credentials.get("bot_token") or "").strip()
        if not token:
            raise ValueError("bot_token is required")
        return {"bot_token": token}

    def verify_and_resolve(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        binding: Mapping[str, Any],
    ) -> ChannelInbound:
        payload = verify_official_signature_and_decode(
            public_key=str(binding["channel_config"]["signing_public_key"]),
            headers=headers,
            raw_body=raw_body,
        )
        if not payload.get("text"):
            raise APIError(
                code="EXAMPLE_BAD_PAYLOAD",
                message="message text is required",
                status_code=400,
            )
        return ChannelInbound(
            content=str(payload["text"]),
            dedup_key=str(payload["event_id"]),
            conversation_key=str(payload["conversation_id"]),
            reply_context={"conversation_id": str(payload["conversation_id"])},
            participant=str(payload.get("sender_id") or "") or None,
            attention=ChannelAttention(
                is_direct_message=bool(payload.get("is_direct")),
                is_mention=bool(payload.get("mentions_agent")),
            ),
        )
```

`verify_and_resolve()` runs synchronously inside the callback request. Header
names are lower case. Verify the raw bytes before decoding them and raise an
`APIError` with status `401` when authentication fails. The example's
`verify_official_signature_and_decode()` represents the exact verification
function required by the messaging product; implement it from that product's
official specification.

## Deliver Agent replies

Implement `deliver_outbound()` to send the final Agent reply. AstraBox passes a
freshly loaded `binding`; its `channel_credentials` contains the write-only
values. `reply_context` is persisted for retry, so it must contain routing IDs
only and never credentials.

Return a `ChannelDeliveryReceipt` with every platform message ID created or
updated. AstraBox stores those IDs before marking delivery complete so inbound
replies can resolve their `reference`. A receipt alone does not make the
external send idempotent: if sending succeeds but receipt persistence fails,
the delivery may be attempted again. Use the product API's idempotency support
when available.

For products that update a message while the Agent works, set
`supports_streaming_delivery = True` and implement `open_delivery()`. Apply the
`turn_started`, `progress`, `settled`, and `failed` events. The
`prior_aliases` argument contains platform message IDs saved by an earlier
attempt; update those messages instead of creating replacements.

Every optional capability flag must be implemented together with its matching
method. Registration rejects partial capability shapes.

## Verify the integration

Bind the reusable provider checks in the plugin test suite:

```python
from astrabox.testing.provider_conformance import ChannelProviderContractSuite

from astrabox_channel_example import ExampleChannelProvider


class TestExampleChannelProvider(ChannelProviderContractSuite):
    def make_provider(self):
        return ExampleChannelProvider()
```

Add fixtures captured from the messaging product for signature verification,
payload variants, event redelivery, reply references, and outbound API errors.
Then verify the complete path:

1. The adapter appears in the channel setup page with the expected fields.
2. A signed event creates or continues the expected conversation.
3. A repeated event ID does not start another turn.
4. Mentions and direct messages follow the Deployment's `attention_policy`.
5. The Agent reply reaches the original conversation or thread.
6. The service reports an explicit delivery error when the product API rejects
   a reply.

## Related

- [Messaging platforms](./channels.md) — setup and message behavior
- [Deployments](./deployments.md) — channel, webhook, and schedule triggers
- [HTTP API](./api.md) — Deployment and trigger routes
