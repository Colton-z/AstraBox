"""The credential an MCP client holds: what it grants, and what it stops granting.

Two properties carry the security of this feature and both are asserted against
the real service rather than a description of it: a key narrowed to `read`
cannot spend, and a revoked key stops working on the next call rather than at
some expiry.

The rest pins the shape a leaked database must have — a digest, never a secret.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent.agent_mcp_service import AgentMCPService
from astrabox.core.service.orchestrator.mcp_client_token_service import (
    SCOPE_CONVERSE,
    SCOPE_READ,
    SECRET_PREFIX,
    MCPClientTokenService,
    digest_secret,
    require_scope_for_tool,
)
from astrabox.persistence.repository.mcp_client_token_repository import MCPClientTokenRepository
from astrabox.persistence.sql import fetch_one
from astrabox.persistence.sql.models.mcp import MCPClientToken

pytestmark = pytest.mark.usefixtures("database")

_TOKENS = MCPClientToken.__table__


_ALICE = UserContext(user_id="u-alice")
_BOB = UserContext(user_id="u-bob")


def _service() -> MCPClientTokenService:
    return MCPClientTokenService(MCPClientTokenRepository())


# --- what the key grants ----------------------------------------------------


async def test_a_read_key_cannot_send_a_message() -> None:
    """The scope split's whole point: reporting without spending."""
    service = _service()
    issued = await service.issue(_ALICE, name="dashboard", scope=SCOPE_READ)

    user, scope = await service.resolve(issued["secret"])
    assert user.user_id == "u-alice"
    assert scope == SCOPE_READ

    # Reading is what it is for.
    require_scope_for_tool(scope, "list_agents")
    require_scope_for_tool(scope, "get_status")

    for spending_tool in (
        "create_conversation",
        "send_message",
        "answer_interaction",
        "cancel_task",
    ):
        with pytest.raises(APIError) as caught:
            require_scope_for_tool(scope, spending_tool)
        envelope = caught.value.to_error_envelope()
        assert envelope["code"] == "MCP_TOKEN_SCOPE_INSUFFICIENT"
        # 403, not 401: the credential is fine and the permission is not, so a
        # holder must not be sent to replace a key that works.
        assert envelope["status_code"] == 403
        assert envelope["owner"] == "client"
        assert envelope["category"] == "auth"


async def test_the_gate_is_wired_at_the_dispatch_point_not_only_available() -> None:
    """The scope function being correct is not the scope check running.

    Driven through `handle_tool_call`, which is the one place every tool passes
    through, with an agent service that raises if it is reached. A gate that
    was removed from the dispatch — or added to the route instead, where a tool
    reached another way would miss it — fails here and nowhere else.
    """

    class _Exploding:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the scope gate let {name} through")

    service = AgentMCPService(agent_service=_Exploding())

    with pytest.raises(APIError) as caught:
        await service.handle_tool_call(
            _ALICE,
            "send_message",
            {"agent_id": "a-1", "session_id": "s-1", "instruction": "hi"},
            scope=SCOPE_READ,
        )

    assert caught.value.code == "MCP_TOKEN_SCOPE_INSUFFICIENT"


# --- what stops granting ----------------------------------------------------


async def test_a_revoked_key_stops_working_on_the_next_call() -> None:
    service = _service()
    issued = await service.issue(_ALICE, name="laptop", scope=SCOPE_CONVERSE)
    secret = issued["secret"]

    await service.resolve(secret)  # live

    await service.revoke(_ALICE, issued["token_id"])

    with pytest.raises(APIError) as caught:
        await service.resolve(secret)
    envelope = caught.value.to_error_envelope()
    # Revoked answers what an unknown secret answers. Telling them apart tells
    # whoever is probing which one they hold, and changes nothing a caller does.
    assert envelope["code"] == "UNAUTHORIZED"
    assert envelope["status_code"] == 401


async def test_an_unknown_secret_is_refused_without_naming_why() -> None:
    service = _service()
    for secret in (SECRET_PREFIX + "never-issued", "not-even-prefixed", ""):
        with pytest.raises(APIError) as caught:
            await service.resolve(secret)
        assert caught.value.code == "UNAUTHORIZED"


async def test_revoking_another_users_key_answers_not_found() -> None:
    """404 rather than 403: 'forbidden' confirms the id exists."""
    service = _service()
    issued = await service.issue(_ALICE, name="alice's", scope=SCOPE_READ)

    with pytest.raises(APIError) as caught:
        await service.revoke(_BOB, issued["token_id"])
    envelope = caught.value.to_error_envelope()
    assert envelope["code"] == "MCP_TOKEN_NOT_FOUND"
    assert envelope["status_code"] == 404

    # And Alice's key is untouched by Bob having asked.
    await service.resolve(issued["secret"])


# --- what a leaked database holds -------------------------------------------


async def test_the_row_holds_a_digest_and_never_the_secret() -> None:
    service = _service()
    issued = await service.issue(_ALICE, name="laptop", scope=SCOPE_CONVERSE)
    secret = issued["secret"]

    row = await fetch_one(select(_TOKENS).where(_TOKENS.c.token_id == issued["token_id"]))

    assert row is not None
    stored = " ".join(str(value) for value in row.values())
    assert secret not in stored, "the secret must not be recoverable from the row"
    assert row["secret_digest"] == digest_secret(secret)


async def test_the_secret_is_returned_once_and_never_listed() -> None:
    service = _service()
    issued = await service.issue(_ALICE, name="laptop", scope=SCOPE_CONVERSE)
    assert issued["secret"].startswith(SECRET_PREFIX)

    listed = await service.list(_ALICE)

    assert [row["token_id"] for row in listed] == [issued["token_id"]]
    # Absent, not redacted: a field that is missing cannot be mistaken for one
    # that happens to be empty.
    assert "secret" not in listed[0]
    assert "secret_digest" not in listed[0]
    assert listed[0]["scope"] == SCOPE_CONVERSE


async def test_a_users_list_holds_only_their_own() -> None:
    service = _service()
    await service.issue(_ALICE, name="alice's", scope=SCOPE_READ)
    await service.issue(_BOB, name="bob's", scope=SCOPE_READ)

    assert [row["name"] for row in await service.list(_ALICE)] == ["alice's"]
    assert [row["name"] for row in await service.list(_BOB)] == ["bob's"]


async def test_an_unusable_scope_or_name_is_refused_at_issuance() -> None:
    service = _service()
    for kwargs in (
        {"name": "", "scope": SCOPE_READ},
        {"name": "x" * 201, "scope": SCOPE_READ},
        {"name": "ok", "scope": "admin"},
    ):
        with pytest.raises(APIError) as caught:
            await service.issue(_ALICE, **kwargs)  # type: ignore[arg-type]
        assert caught.value.code == "INVALID_REQUEST"

    with pytest.raises(APIError):
        await service.issue(_ALICE, name="ok", scope=SCOPE_READ, expires_in_days=0)
