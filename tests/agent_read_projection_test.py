"""What an Agent read hands out, and what it must never hand out.

Two drop sets had drifted apart. ``extension_catalog`` — a runtime snapshot
the Agent-extension API owns — was documented in one reader as an internal
the Agent form "must neither expose nor write", and returned by the other.
``conversation_uid_cursor``, the shared box's UID allocation cursor owned by
``conversation_identity``'s compare-and-set, was returned by both: it carries
no leading underscore, so the rule that catches the other internals never saw
it.

The write side is deliberately NOT symmetric with this, and that asymmetry is
load-bearing rather than an oversight: the authoring payload is closed and
refuses server-owned keys instead of ignoring them, so a caller cannot come
away believing an access-policy or ownership change took effect
(``agent_access_control_update_test`` states the reasoning). A client that
reads an Agent and writes part of it back therefore strips what it does not
own. The fields pinned here never reach it to strip — which is the point:
they are not the client's business at all.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.core.service.orchestrator.agent_config_service import (
    AgentConfigService,
)


def _stored_agent() -> dict[str, Any]:
    """One row as the store holds it: public shape plus what has leaked in."""

    return {
        "agent_id": "agent-1",
        "name": "Investment Research",
        "system": "Be terse.",
        "model": "claude-opus-4",
        "environment_name": "astrabox-e2e-investment",
        "enabled": True,
        "visibility": "private",
        # Public to read; written by PUT /admin/agents/{id}/mcp-servers.
        "mcp_assignments": [{"provider": "builtin", "item_id": "mcp_fetch"}],
        # Internals no client may see.
        "conversation_uid_cursor": 2007,
        "workspace_id": "ws-platform-owned",
        "extension_catalog": {"snapshot": "opaque"},
        "credential_vault_ids": ["vault-1"],
        "credentials_updated_by": "user-1",
        "credentials_updated_at": "2026-08-25T00:00:00+00:00",
        "plugin_mcp_bridge_allowlist": ["everything"],
        "main_session_id": "session-1",
        "_agent_prewarm_fingerprint": "fp-1",
        "_prepared_slot": {"state": "prepared"},
    }


#: Named one by one rather than compared against the constant the projection
#: itself uses — that comparison passes whatever the constant says, including
#: nothing.
_MUST_NOT_APPEAR = (
    "conversation_uid_cursor",
    "workspace_id",
    "extension_catalog",
    "credential_vault_ids",
    "credentials_updated_by",
    "credentials_updated_at",
    "plugin_mcp_bridge_allowlist",
    "main_session_id",
)


def test_the_agent_read_hands_out_nothing_internal() -> None:
    seen = AgentService._sanitize(_stored_agent())

    for key in _MUST_NOT_APPEAR:
        assert key not in seen, key
    # The leading-underscore rule still stands on its own.
    assert not [k for k in seen if k.startswith("_")]
    # And it did not take the public shape with it.
    assert seen["name"] == "Investment Research"
    assert seen["visibility"] == "private"
    assert seen["mcp_assignments"] == [
        {"provider": "builtin", "item_id": "mcp_fetch"}
    ]


def test_the_config_read_hands_out_the_same_nothing() -> None:
    """Both readers, one answer.

    The drift this pins was not a missing check but a second copy of the
    answer: whichever reader a caller happened to reach decided what it saw.
    """

    seen = AgentConfigService.sanitize_agent_doc(_stored_agent())

    for key in _MUST_NOT_APPEAR:
        assert key not in seen, key
    assert not [key for key in seen if key.startswith("_")]
    assert seen["name"] == "Investment Research"
