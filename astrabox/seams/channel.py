"""Channel seam — concrete messaging platforms over AstraBox's durable spine.

A **channel provider** turns an external messaging system (an IM platform, a
ticketing tool, or email) into agent conversations. A channel binding is a
Deployment whose ``scene`` is ``channel:<provider-name>``. Providers may accept
authenticated HTTP triggers directly, or host a long-lived source through an
internal adapter runtime. Both converge on the same durable ingress, delivery
outbox, conversation mapping, and source cursor. The provider owns only the
platform-specific concerns:

* **configuration** — a non-secret schema plus write-only credential fields;
  complete bindings are validated before becoming desired state;
* **inbound mapping** — :meth:`ChannelProvider.verify_and_resolve` handles a
  direct callback, while :meth:`ChannelProvider.open_source` yields managed
  source envelopes; each produces :class:`ChannelInbound`;
* **outbound delivery** — :meth:`ChannelProvider.deliver_outbound` posts the
  finished turn's assistant text back to the platform using that
  ``reply_context`` and the freshly loaded binding. The latter keeps provider
  credentials out of the durable outbox;
* **official callbacks** — callback-based adapters may forward an authenticated
  platform exchange through :meth:`ChannelProvider.forward_callback` without
  exposing their internal control plane.

Everything else belongs to core: binding CRUD, encrypted credential storage,
attention policy, deduplication, conversation start, durable ACK ordering,
background turn drive, and recovery.

Registration mirrors every other seam: ``register_channel`` at import +
the ``astrabox.providers.channel`` entry-point group; name lookups fail loud.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

ENTRY_POINT_GROUP = "astrabox.providers.channel"

#: Webhook ``scene`` prefix that routes a binding to a channel provider.
CHANNEL_SCENE_PREFIX = "channel:"


@dataclass(frozen=True)
class ChannelField:
    """One provider-owned field in the channel binding form.

    Provider fields cannot be hard-coded in the console: an installed native
    provider must be able to replace a bundled adapter without teaching the
    frontend another schema. ``secret`` controls storage as well as rendering;
    secret fields are write-only and never enter the deployment repository.
    """

    key: str
    label: str
    required: bool = True
    secret: bool = False
    kind: str = "string"
    options: tuple[str, ...] = ()
    default: Any = None
    placeholder: str = ""
    help: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "secret": self.secret,
            "kind": self.kind,
            "options": list(self.options),
            "default": self.default,
            "placeholder": self.placeholder,
            "help": self.help,
        }


@dataclass(frozen=True)
class ChannelDescriptor:
    """Non-secret product metadata exposed by one channel provider."""

    name: str
    label: str
    config_fields: tuple[ChannelField, ...] = ()
    credential_fields: tuple[ChannelField, ...] = ()
    callback_path: str | None = None
    setup_url: str | None = None
    documentation_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "scene": f"{CHANNEL_SCENE_PREFIX}{self.name}",
            "config_fields": [field.to_dict() for field in self.config_fields],
            "credential_fields": [
                field.to_dict() for field in self.credential_fields
            ],
            "callback_path": self.callback_path,
            "setup_url": self.setup_url,
            "documentation_url": self.documentation_url,
        }


@dataclass
class ChannelAttention:
    """Typed attention signals for the core-owned respond/ignore policy.

    Providers only REPORT what the platform said (was the agent DM'd,
    @-mentioned, replied to?); whether the agent responds is decided in core
    by the binding's attention policy (docs/channel-spine.md) — never in the
    provider plugin.
    """

    is_direct_message: bool = False
    is_mention: bool = False
    is_reply_to_agent: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "is_direct_message": bool(self.is_direct_message),
            "is_mention": bool(self.is_mention),
            "is_reply_to_agent": bool(self.is_reply_to_agent),
        }

    @property
    def any_signal(self) -> bool:
        return bool(
            self.is_direct_message or self.is_mention or self.is_reply_to_agent
        )


@dataclass
class ChannelAliasLink:
    """A post-delivery alias enrichment: "``new_alias`` is the same platform
    message as ``existing_alias``".

    Some platforms materialize the id users actually quote only AFTER the
    delivery ack (e.g. an ``openMsgId`` arriving on the robot's own message
    echo, paired with the delivery's ``carrierId``). The provider observes
    that correlation on its inbound path and hands it to the spine as an
    enrichment instead of a message: the spine attaches ``new_alias`` to the
    conversation chain ``existing_alias`` already belongs to — idempotently,
    and never creating a chain that does not exist.
    """

    #: An alias already persisted by a delivery receipt (chain anchor).
    existing_alias: str
    #: The late-materialized id future inbound ``reference``\ s will carry.
    new_alias: str


@dataclass
class ChannelRecall:
    """A provider-identified deletion, separate from a user message."""

    message_id: str
    timestamp: str


@dataclass
class ChannelInbound:
    """What a verified inbound callback resolves to."""

    #: The message the agent turn runs with.
    content: str
    #: Opaque reply routing data, handed back to ``deliver_outbound``
    #: unchanged once the turn finishes. ``None`` → no outbound delivery.
    reply_context: dict[str, Any] | None = None
    #: Extra fields merged into the trigger's HTTP response (e.g. a
    #: platform-required ack shape). Never carries secrets.
    ack_extra: dict[str, Any] = field(default_factory=dict)
    #: Platform message id for at-least-once inbound delivery. When set, the
    #: spine claims it once per binding: a redelivery of the same key gets an
    #: idempotent ``status="duplicate"`` ack (with the original session_id)
    #: instead of a second conversation/sandbox/reply. ``None`` → every
    #: callback is a distinct message, but it still becomes a durable,
    #: crash-recoverable work item.
    dedup_key: str | None = None
    #: Platform conversation identity (chat id, thread ts, ticket id…). When
    #: set, the spine routes the message into the SAME agent session for the
    #: binding+key (creating it on first sight, replacing it if terminated)
    #: instead of starting a fresh session per message. ``None`` → one-shot
    #: session per callback.
    conversation_key: str | None = None
    #: Typed attention signals for the core-owned respond/ignore policy.
    #: ``None`` → the platform has no attention concept; the message is
    #: always eligible.
    attention: ChannelAttention | None = None
    #: Platform message id this inbound replies to/quotes, if any. Core maps
    #: it onto the conversation chain (reply-chain identity).
    reference: str | None = None
    #: External sender identity (a Slack member id, a phone number…) — an
    #: execution subject recorded on the work item, never an owner.
    participant: str | None = None
    #: Post-delivery alias enrichment. When set, this inbound is NOT a
    #: message: the spine performs the enrichment and never starts a turn
    #: (``content`` is ignored). Arrives through the same single ingress as
    #: everything else — HTTP callback or broker source alike.
    alias_link: ChannelAliasLink | None = None
    #: A provider-level terminal classification for an authenticated event
    #: that must never drive a turn (the connected bot's own message echo, a
    #: login event, …). Core persists the classification before acknowledging
    #: the source. This is transport meaning, not attention policy.
    ignore_reason: str | None = None
    #: Original message time for retained conversation context. Providers with
    #: no external context plane leave this unset (ordinary webhooks stay opaque).
    message_timestamp: str | None = None
    #: Whether ordinary messages participate in later conversation catch-up.
    retain_context: bool = False
    #: A recall records external state; it never submits a turn.
    recall: ChannelRecall | None = None


#: Version stamped on every :class:`ChannelEvent` — the stable, versioned
#: projection providers consume (docs/channel-spine.md invariant D). This is
#: a forward contract for third-party providers, never a compatibility layer:
#: raw internal AI-SDK frames must not cross the seam.
CHANNEL_EVENT_VERSION = 1

EVENT_TURN_STARTED = "turn_started"
EVENT_PROGRESS = "progress"
EVENT_SETTLED = "settled"
EVENT_FAILED = "failed"


@dataclass
class ChannelEvent:
    """One step of a delivery session, projected from the turn's durable
    frame log by the spine's deliverer.

    ``progress`` carries the coalesced assistant text SO FAR (idempotent
    update-the-card semantics — intermediate steps may be skipped);
    ``settled``/``failed`` are terminal and arrive exactly once per delivery
    session. ``seq`` is monotonic within the session.
    """

    type: str
    seq: int
    command_id: str
    turn_id: str
    text: str | None = None
    error: str | None = None
    version: int = CHANNEL_EVENT_VERSION


@dataclass
class ChannelDeliveryReceipt:
    """Platform message ids/aliases a delivery created or updated.

    The spine persists these against the conversation chain BEFORE the
    delivery is marked complete — they are the idempotency evidence a
    crash-resumed delivery updates instead of re-creating, and the targets
    inbound ``reference``\\ s resolve against (reply-chain identity).
    """

    message_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChannelCallbackResponse:
    """Raw provider callback response returned to the messaging platform."""

    status_code: int
    headers: dict[str, str]
    body: bytes


class ChannelDeliveryHandle(ABC):
    """One streaming delivery session (a live card/message being updated)."""

    @abstractmethod
    async def emit(self, event: ChannelEvent) -> ChannelDeliveryReceipt | None:
        """Apply one event to the platform; return any new/updated aliases.

        The spine calls this sequentially per session — never concurrently —
        and persists returned aliases before the next emit. A raise fails the
        delivery attempt (retried under the outbox policy), never the turn.
        """

    async def close(self) -> None:
        """Release transport resources. Idempotent; called exactly once."""
        return None


class ChannelSourceEnvelope(ABC):
    """One source-delivered inbound message awaiting acknowledgement."""

    #: The channel binding this message belongs to.
    deployment_id: str = ""
    #: The decoded, transport-authenticated inbound.
    inbound: ChannelInbound
    #: Optional monotonic source sequence. The source host persists it only
    #: after :meth:`ChannelIngressService.ingest` has durably claimed the
    #: message, then supplies it to ``open_source`` after a reconnect. A
    #: provider without resumable ordered delivery leaves this as ``None``.
    source_cursor: int | None = None

    @abstractmethod
    async def ack(self, receipt: Any) -> None:
        """Acknowledge upstream. Called ONLY after the spine's ingest
        returned a durable receipt (docs/channel-spine.md invariant A)."""

    @abstractmethod
    async def nack(self, error: str) -> None:
        """Refuse without consuming — the broker redelivers later."""


class ChannelProvider(ABC):
    """One messaging-platform integration (see module docstring)."""

    #: Registry key; the binding's scene is ``channel:<name>``.
    name: str = ""

    #: Optional capability: the provider can stream a delivery session
    #: (:meth:`open_delivery`) consuming :class:`ChannelEvent`\ s. The
    #: final-text :meth:`deliver_outbound` remains the simple default.
    supports_streaming_delivery: bool = False

    #: Optional capability: the provider hosts a long-lived broker consumer
    #: (:meth:`open_source`) yielding typed envelopes; the spine ingests and
    #: acks each one after the durable claim.
    supports_source: bool = False

    #: Whether Deployment CRUD issues the ordinary trigger shared secret.
    #: Broker-backed providers authenticate upstream with their write-only
    #: credential schema instead and set this false.
    uses_trigger_secret: bool = True

    def describe(self) -> ChannelDescriptor:
        """Describe the provider's concrete product surface.

        The default keeps third-party providers source-compatible: their scene
        remains creatable, with no provider-specific fields, until they opt in
        to the schema-driven console.
        """

        return ChannelDescriptor(name=self.name, label=self.name)

    @abstractmethod
    def verify_and_resolve(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        binding: Mapping[str, Any],
    ) -> ChannelInbound:
        """Authenticate the callback and map it onto a turn message.

        ``binding`` is the webhook row (``secret``, ``prompt_prefix``,
        ``agent_id``, …). Raise
        :class:`~astrabox.common.utils.errors.APIError` (401) on auth failure
        — never return a guess. Headers arrive lower-cased.
        """

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize the binding's non-secret provider config.

        Provider credentials belong in the binding's write-only credential
        mapping; this mapping is for non-secret routing and behaviour.
        The default provider has no configuration surface and therefore fails
        loudly when fields are supplied.
        """
        if config:
            raise ValueError(
                f"channel provider {self.name!r} does not accept channel_config"
            )
        return {}

    def normalize_credentials(
        self, credentials: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate write-only credentials before encrypted storage.

        The default rejects every field. A provider that advertises credential
        fields must override this method; the normalized mapping is hydrated
        only for source/open-delivery calls and is never returned by an API.
        """

        if credentials:
            raise ValueError(
                f"channel provider {self.name!r} does not accept credentials"
            )
        return {}

    async def validate_configuration(
        self,
        *,
        config: Mapping[str, Any],
        credentials: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate one complete binding before it becomes desired state.

        The default composes the provider's synchronous normalizers. Providers
        backed by an internal adapter runtime may override this to execute that
        runtime's authoritative schema; Deployment CRUD awaits the result, so a
        binding never becomes enabled first and discovers bad credentials only
        in a background reconnect loop.
        """

        return self.normalize_config(config), self.normalize_credentials(credentials)

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
        """Forward a platform webhook to this provider's managed connector."""

        _ = (deployment_id, method, path, query, headers, raw_body)
        raise NotImplementedError(
            f"channel provider {self.name!r} has no managed callback endpoint"
        )

    async def deliver_outbound(
        self,
        *,
        reply_context: dict[str, Any],
        text: str,
        binding: Mapping[str, Any],
    ) -> "ChannelDeliveryReceipt | None":
        """Send the finished turn's assistant text back to the platform.

        Default: no-op (inbound-only channel). Called in the background after
        the turn settles; exceptions are logged by the caller, never raised
        into the turn. Returning a :class:`ChannelDeliveryReceipt` gets the
        platform aliases persisted onto the conversation chain before the
        delivery is marked complete.

        ``binding`` is reloaded for every durable delivery attempt. Providers
        may read its credential but must never copy that credential into
        ``reply_context`` (which is persisted in the outbox).
        """
        _ = (reply_context, text, binding)
        return None

    async def open_delivery(
        self,
        *,
        reply_context: dict[str, Any],
        prior_aliases: list[str],
        binding: Mapping[str, Any],
    ) -> ChannelDeliveryHandle:
        """Open a streaming delivery session (``supports_streaming_delivery``).

        ``prior_aliases`` carries aliases a previous (crashed) attempt already
        created — the handle must update those messages instead of creating
        duplicates. ``binding`` is freshly loaded so credentials never need to
        enter the durable delivery row.
        """
        raise NotImplementedError(
            f"channel provider {self.name!r} does not stream deliveries"
        )

    def open_source(self, *, binding: Mapping[str, Any]) -> Any:
        """Open one binding's long-lived inbound source.

        Returns an async iterator of :class:`ChannelSourceEnvelope`. The
        source host opens one iterator per enabled binding, reopens it with a
        freshly loaded binding after failure, and passes the binding's durable
        ``source_cursor`` so resumable transports can request replay.
        """
        _ = binding
        raise NotImplementedError(
            f"channel provider {self.name!r} does not host a source"
        )


_CHANNELS: dict[str, ChannelProvider] = {}


def _validate_channel_capabilities(provider: ChannelProvider, name: str) -> None:
    """Reject partial capability shapes at registration, not at callback time.

    Mirrors the storage/sandbox seams: an opt-in flag must come with its
    complete method shape, and an overridden capability method must come with
    its flag — a provider that half-implements streaming or sourcing would
    otherwise fail on the first real message.
    """
    for flag_name in ("supports_streaming_delivery", "supports_source"):
        flag = getattr(provider, flag_name, False)
        if not isinstance(flag, bool):
            raise RuntimeError(
                f"channel provider {name!r}: {flag_name} must be a bool"
            )
    if not isinstance(getattr(provider, "uses_trigger_secret", None), bool):
        raise RuntimeError(
            f"channel provider {name!r}: uses_trigger_secret must be a bool"
        )
    descriptor = provider.describe()
    if not isinstance(descriptor, ChannelDescriptor):
        raise RuntimeError(
            f"channel provider {name!r}: describe() must return ChannelDescriptor"
        )
    if descriptor.name != name:
        raise RuntimeError(
            f"channel provider {name!r}: descriptor name must equal registry name"
        )
    fields = (*descriptor.config_fields, *descriptor.credential_fields)
    if any(not isinstance(field, ChannelField) or not field.key for field in fields):
        raise RuntimeError(
            f"channel provider {name!r}: every descriptor field must have a key"
        )
    keys = [field.key for field in fields]
    if len(keys) != len(set(keys)):
        raise RuntimeError(
            f"channel provider {name!r}: descriptor field keys must be unique"
        )
    allowed_kinds = {"string", "number", "boolean", "select"}
    if any(field.kind not in allowed_kinds for field in fields):
        raise RuntimeError(
            f"channel provider {name!r}: descriptor field kind must be one of "
            f"{sorted(allowed_kinds)}"
        )
    if any(field.kind == "select" and not field.options for field in fields):
        raise RuntimeError(
            f"channel provider {name!r}: select fields must declare options"
        )
    if any(field.secret for field in descriptor.config_fields):
        raise RuntimeError(
            f"channel provider {name!r}: config fields cannot be secret; put them "
            "in credential_fields so they never enter the Deployment record"
        )
    if any(not field.secret for field in descriptor.credential_fields):
        raise RuntimeError(
            f"channel provider {name!r}: credential fields must be secret"
        )
    if descriptor.credential_fields and provider.uses_trigger_secret:
        raise RuntimeError(
            f"channel provider {name!r}: credential fields and a trigger secret "
            "are two competing credential models"
        )
    for label, candidate in (
        ("setup_url", descriptor.setup_url),
        ("documentation_url", descriptor.documentation_url),
    ):
        if candidate is None:
            continue
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(
                f"channel provider {name!r}: {label} must be an absolute HTTP(S) URL"
            )
    callback_path = descriptor.callback_path
    has_callback = callback_path is not None
    if callback_path is not None and (
        not callback_path.startswith("/")
        or "?" in callback_path
        or "#" in callback_path
    ):
        raise RuntimeError(
            f"channel provider {name!r}: callback_path must be an absolute URL path"
        )
    overrides_callback = (
        type(provider).forward_callback is not ChannelProvider.forward_callback
    )
    if has_callback and not overrides_callback:
        raise RuntimeError(
            f"channel provider {name!r}: callback_path requires an implemented "
            "forward_callback"
        )
    if has_callback and not inspect.iscoroutinefunction(type(provider).forward_callback):
        raise RuntimeError(
            f"channel provider {name!r}: forward_callback must be async"
        )
    streams = bool(provider.supports_streaming_delivery)
    overrides_delivery = (
        type(provider).open_delivery is not ChannelProvider.open_delivery
    )
    if streams != overrides_delivery:
        raise RuntimeError(
            f"channel provider {name!r}: supports_streaming_delivery="
            f"{streams} but open_delivery is "
            f"{'not ' if not overrides_delivery else ''}overridden — the "
            "streaming capability must be declared and implemented together"
        )
    if streams and not inspect.iscoroutinefunction(type(provider).open_delivery):
        raise RuntimeError(
            f"channel provider {name!r}: open_delivery must be async"
        )
    if streams and "binding" not in inspect.signature(
        type(provider).open_delivery
    ).parameters:
        raise RuntimeError(
            f"channel provider {name!r}: open_delivery must accept a binding"
        )
    delivery = type(provider).deliver_outbound
    if not inspect.iscoroutinefunction(delivery):
        raise RuntimeError(
            f"channel provider {name!r}: deliver_outbound must be async"
        )
    if "binding" not in inspect.signature(delivery).parameters:
        raise RuntimeError(
            f"channel provider {name!r}: deliver_outbound must accept a binding"
        )
    if not inspect.iscoroutinefunction(type(provider).validate_configuration):
        raise RuntimeError(
            f"channel provider {name!r}: validate_configuration must be async"
        )
    sources = bool(provider.supports_source)
    overrides_source = type(provider).open_source is not ChannelProvider.open_source
    if sources != overrides_source:
        raise RuntimeError(
            f"channel provider {name!r}: supports_source={sources} but "
            f"open_source is {'not ' if not overrides_source else ''}"
            "overridden — the source capability must be declared and "
            "implemented together"
        )
    if sources and "binding" not in inspect.signature(type(provider).open_source).parameters:
        raise RuntimeError(
            f"channel provider {name!r}: open_source must accept a binding"
        )


def register_channel(provider: ChannelProvider) -> None:
    """Register a provider under its ``name``. Last registration wins.

    Capability shapes are validated HERE (fail-loud at import/install), so a
    binding can trust every registered provider's declared capabilities.
    """
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("channel provider must have a non-empty name")
    _validate_channel_capabilities(provider, name)
    _CHANNELS[name] = provider


def channel_if_registered(name: str) -> ChannelProvider | None:
    return _CHANNELS.get(str(name or "").strip().lower())


def registered_channels() -> dict[str, ChannelProvider]:
    """A snapshot of the registry."""
    return dict(_CHANNELS)


def get_channel(name: str) -> ChannelProvider:
    """Fail-loud name lookup (mirrors every other seam registry)."""
    provider = channel_if_registered(name)
    if provider is None:
        raise RuntimeError(
            f"no channel provider named {name!r} is registered "
            f"(registered: {sorted(_CHANNELS)}); install its distribution "
            f"(entry-point group {ENTRY_POINT_GROUP!r}) or fix the binding's scene"
        )
    return provider


def channel_scene_name(scene: str) -> str | None:
    """``channel:<name>`` → ``<name>``; anything else → ``None``."""
    text = str(scene or "").strip()
    if text.startswith(CHANNEL_SCENE_PREFIX):
        return text[len(CHANNEL_SCENE_PREFIX) :].strip().lower() or None
    return None
