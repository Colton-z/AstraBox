"""Channel provider registration refuses what would expose a secret or an identity gap.

``register_channel`` refuses a secret in a deployment's plain configuration, a
setup link that is not a safe URL, and an inbound signal that does not carry the
identity its role needs.
"""

from __future__ import annotations

import pytest

from astrabox.seams.channel import (
    SIGNAL_AUTHORIZATION,
    SIGNAL_OBSERVATION,
    ChannelAliasLink,
    ChannelAttention,
    ChannelDescriptor,
    ChannelField,
    ChannelInbound,
    ChannelProvider,
    ChannelRecall,
    register_channel,
)


# ── registration gating (fail-loud on partial shapes) ────────────────────────


def test_registration_rejects_secret_in_deployment_config() -> None:
    class _PersistedSecret(ChannelProvider):
        name = "conf_persisted_secret"

        def describe(self) -> ChannelDescriptor:
            return ChannelDescriptor(
                name=self.name,
                label="bad",
                config_fields=(ChannelField(key="token", label="Token", secret=True),),
            )

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    with pytest.raises(RuntimeError, match="config fields cannot be secret"):
        register_channel(_PersistedSecret())


def test_registration_rejects_unsafe_setup_link() -> None:
    class _UnsafeLink(ChannelProvider):
        name = "conf_unsafe_link"

        def describe(self) -> ChannelDescriptor:
            return ChannelDescriptor(
                name=self.name,
                label="bad",
                setup_url="javascript:alert(1)",
            )

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    with pytest.raises(RuntimeError, match=r"absolute HTTP\(S\) URL"):
        register_channel(_UnsafeLink())


# ── inbound signals carry the identity their role needs ─────────────────────


def test_inbound_signals_carry_the_identity_their_role_needs() -> None:
    """An observation and an authorization meet on one record, so each must
    name its message and sender, and neither may carry what the other owns:
    an observation has no destination to answer, an authorization no content
    to run. A malformed signal fails when the provider builds it."""
    mention = ChannelAttention(is_mention=True)
    observed = {
        "content": "the message", "dedup_key": "m-1", "participant": "u-1",
        "conversation_key": "room", "message_timestamp": "2026-10-01T00:00:00Z",
        "retain_context": True, "signal": SIGNAL_OBSERVATION,
    }
    authorized = {
        "content": "", "dedup_key": "m-1", "participant": "u-1",
        "attention": mention, "reply_context": {"cb": "x"},
        "signal": SIGNAL_AUTHORIZATION,
    }
    assert ChannelInbound(**observed).signal == SIGNAL_OBSERVATION
    assert ChannelInbound(**authorized).signal == SIGNAL_AUTHORIZATION
    for invalid in [
        {**observed, "signal": "trigger"},
        {**observed, "dedup_key": None},
        {**observed, "participant": None},
        {**observed, "retain_context": False},
        {**observed, "message_timestamp": None},
        {**observed, "reply_context": {"cb": "x"}},
        {**authorized, "content": "the message"},
        {**authorized, "reference": "m-0"},
        {**authorized, "attention": None},
        {**authorized, "attention": ChannelAttention()},
        {**authorized, "participant": None},
        {**observed, "recall": ChannelRecall(message_id="m-0", timestamp="t")},
        {**authorized, "alias_link": ChannelAliasLink(existing_alias="a", new_alias="b")},
        {**authorized, "action": "ix1:0000000000000000:0:1"},
    ]:
        with pytest.raises(ValueError):
            ChannelInbound(**invalid)
