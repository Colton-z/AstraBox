"""The credential an MCP client holds: what it grants, and what it stops granting.

Two properties carry the security of this feature and both are asserted against
the real service rather than a description of it: a key narrowed to `read`
cannot spend, and a revoked key stops working on the next call rather than at
some expiry.

The rest pins the shape a leaked database must have — a digest, never a secret.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

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
from astrabox.persistence.repository.mcp_client_token_repository import (
    MCP_CLIENT_TOKENS_COLLECTION,
    MCPClientTokenRepository,
)
from astrabox.persistence.repository.backend import get_async_collection


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from astrabox.config.settings import get_settings

    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


async def test_a_converse_key_reaches_every_tool() -> None:
    """The other side of the same fence — a scope that refused everything
    would satisfy the test above on its own."""
    service = _service()
    issued = await service.issue(_ALICE, name="cursor", scope=SCOPE_CONVERSE)
    _user, scope = await service.resolve(issued["secret"])

    for tool in (
        "list_agents",
        "get_status",
        "create_conversation",
        "send_message",
        "answer_interaction",
        "cancel_task",
    ):
        require_scope_for_tool(scope, tool)


async def test_a_full_identity_is_not_narrowed() -> None:
    """`None` is a person who signed in, not an absent restriction."""
    for tool in ("list_agents", "send_message"):
        require_scope_for_tool(None, tool)


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


async def test_an_expired_key_says_so_because_the_recovery_differs() -> None:
    service = _service()
    issued = await service.issue(_ALICE, name="old", scope=SCOPE_CONVERSE)
    collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)
    await collection.update_one(
        {"token_id": issued["token_id"]},
        {"$set": {"expires_at": "2000-01-01T00:00:00Z"}},
    )

    with pytest.raises(APIError) as caught:
        await service.resolve(issued["secret"])

    assert caught.value.to_error_envelope()["code"] == "TOKEN_EXPIRED"


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

    collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)
    row = await collection.find_one({"token_id": issued["token_id"]})

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


async def test_send_message_refuses_a_blank_instruction_before_a_turn_exists() -> None:
    """The argument that carries the message is enforced where it is named.

    Unenforced, an empty instruction travels unexamined into the turn worker,
    which creates a turn, dispatches it, and fails it — so a client that got the
    field name wrong is answered with a FAILED turn and "content is empty" from
    four layers down, rather than being told which argument it got wrong. The
    agent service explodes on any attribute access, so this also pins that the
    refusal happens before anything downstream is touched.
    """
    class _AgentRepo:
        async def get_agent(self, agent_id: str) -> dict[str, Any]:
            return {"agent_id": agent_id, "state": "ACTIVE", "access": "public"}

    class _VisibleAgentNoTurns:
        """Visible, active, and unable to start a turn.

        The access check legitimately runs first — an argument must not be
        validated for an agent the caller cannot see — so the double satisfies
        that and explodes on the platform instead. Reaching `_platform` is
        precisely the failure this test exists to catch: it is where a turn gets
        created.
        """

        _agent_repo = _AgentRepo()

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"a blank instruction reached {name}")

    service = AgentMCPService(agent_service=_VisibleAgentNoTurns())

    with pytest.raises(APIError) as caught:
        await service.handle_tool_call(
            _ALICE,
            "send_message",
            {"agent_id": "a-1", "session_id": "s-1", "message": "wrong field name"},
            scope="converse",
        )

    assert caught.value.code == "INVALID_REQUEST"
    assert "instruction" in caught.value.message


async def test_send_message_hands_its_drain_to_the_managed_spawner() -> None:
    """The turn stream outlives the tool call, so its consumer must too.

    `send_message` answers SUBMITTED and returns while the engine is still
    working, which leaves the drain running past its caller. A bare
    `asyncio.ensure_future` cannot do that safely: the task inherits the
    request's context and is cancelled when the call returns, and nothing holds
    a reference so it can also be collected mid-flight. The platform's spawner
    exists to fix exactly those two things, and this pins that the MCP path uses
    it rather than re-introducing the primitive it replaced.
    """
    spawned: list[str] = []

    class _Platform:
        def _spawn_background_task(self, coro: Any, *, name: str | None = None) -> Any:
            spawned.append(str(name or ""))
            # Close it rather than running it: this asserts WHERE the drain is
            # handed off, and letting it run would drag the whole turn pipeline
            # into a unit test.
            coro.close()
            return None

        def stream_message_events_ds(self, *args: Any, **kwargs: Any) -> Any:
            async def _one_event() -> Any:
                yield {"type": "start"}
                yield {"type": "finish"}

            return _one_event()

    class _AgentRepo:
        async def get_agent(self, agent_id: str) -> dict[str, Any]:
            return {"agent_id": agent_id, "state": "ACTIVE", "access": "public"}

    class _AgentService:
        _agent_repo = _AgentRepo()
        _platform = _Platform()

    service = AgentMCPService(agent_service=_AgentService())
    service._get_bound_session = AsyncMock(return_value={"session_id": "s-1", "state": "READY"})  # type: ignore[method-assign]
    service._auto_recover_if_needed = AsyncMock(  # type: ignore[method-assign]
        return_value={"session_id": "s-1", "state": "READY"}
    )
    service._wait_for_creation = AsyncMock(  # type: ignore[method-assign]
        return_value={"session_id": "s-1", "state": "READY"}
    )

    result = await service.handle_tool_call(
        _ALICE,
        "send_message",
        {"agent_id": "a-1", "session_id": "s-1", "instruction": "hello"},
        scope="converse",
    )

    assert result["state"] == "SUBMITTED"
    assert spawned == ["mcp-drain-s-1"], (
        "the drain must go through the platform's managed spawner — a bare task "
        "is cancelled with the request that created it"
    )
