"""Artifact-run admission follows the owner, viewer level and origin ACL table."""

import pytest

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.artifacts.origin import origin_authorized
from tests.artifact_service_test import artifact_service as artifact_service


@pytest.mark.parametrize(
    "user_id,level,visibility,allowed,assistant,expected",
    [
        (None, "view", "public", [], False, False),
        ("owner", "owner", "private", [], False, False),
        ("owner", "view", "private", [], False, False),
        ("owner", "owner", "allowlist", ["owner"], False, True),
        ("viewer", "view", "public", [], False, False),
        ("viewer", "interact", "public", [], False, True),
        ("viewer", "interact", "private", [], False, False),
        ("viewer", "interact", "allowlist", ["viewer"], False, True),
        ("viewer", "interact", "allowlist", ["someone"], False, False),
        ("viewer", "interact", "public", [], True, False),
        ("owner", "owner", "private", [], True, True),
    ],
)
def test_agent_run_authorization_table(
    user_id, level, visibility, allowed, assistant, expected
) -> None:
    user = UserContext(user_id) if user_id else None
    origin = {"assistant_id": "assistant"} if assistant else {"agent_id": "agent"}
    agent = {"visibility": visibility, "allowed_user_ids": allowed, "created_by": "creator"}
    assert origin_authorized(user, level, "owner", origin, agent, run=True) is expected


def test_sample_allows_authorized_view_level_without_granting_agent_run() -> None:
    viewer = UserContext("viewer")
    agent = {"visibility": "public"}
    assert origin_authorized(viewer, "view", "owner", {"agent_id": "agent"}, agent, run=False)
    assert not origin_authorized(viewer, "view", "owner", {"agent_id": "agent"}, agent, run=True)


@pytest.mark.parametrize("run", [False, True])
def test_artifact_ownership_never_bypasses_origin_agent_acl(run) -> None:
    assert not origin_authorized(UserContext("owner"), "owner", "owner", {"agent_id": "agent"}, {"visibility": "private", "created_by": "someone-else"}, run=run)


@pytest.mark.asyncio
async def test_owner_artifact_run_uses_ordinary_viewer_conversation(artifact_service) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from astrabox.core.service.artifacts.agent_runs import ArtifactAgentRuns
    from tests.artifact_service_test import OWNER, create_page

    aid = await create_page(artifact_service, {"agent": {}})
    viewer = await artifact_service.resolve_viewer(user=OWNER, artifact_id=aid)
    agent = SimpleNamespace(start_conversation=AsyncMock(return_value={"session_id": "run"}), start_integration_conversation=AsyncMock(return_value={"session_id": "run"}))
    platform = SimpleNamespace(
        _sessions_repo=SimpleNamespace(update_session=AsyncMock()),
        _platform_mcp_binding_repo=SimpleNamespace(attach_session_grant=AsyncMock()),
        session_runtime=SimpleNamespace(send_as=AsyncMock()),
    )
    runs = ArtifactAgentRuns(artifacts=artifact_service, origin_access=SimpleNamespace(authorize=AsyncMock()), platform=platform, agent_service_getter=lambda: agent, assistant_service_getter=lambda: None)
    await runs.start(viewer, "Refresh")
    agent.start_conversation.assert_awaited_once_with(OWNER, "agent-1")
    agent.start_integration_conversation.assert_not_awaited()
