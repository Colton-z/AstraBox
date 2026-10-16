"""JSON-RPC artifact tools preserve the binding user and the grant's resource scope."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from astrabox.core.service.artifacts.service import ArtifactService
from astrabox.core.service.orchestrator.platform_mcp_service import PlatformMCPService
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.artifacts import ArtifactShare
from tests.artifact_service_test import (
    OWNER,
    VIEWER,
    create_page,
)
from tests.artifact_service_test import (
    artifact_service as artifact_service,
)


def mcp_service(
    service: ArtifactService, *, user_id: str = "owner", grant: dict | None = None,
    agent_id: str | None = "agent-1", assistant_id: str | None = None,
    source_type: str = "agent",
) -> PlatformMCPService:
    binding = {
        "session_id": "session",
        "user_id": user_id,
        "scope_kind": "session",
    }
    session = {**binding, "agent_id": agent_id, "source_type": source_type,
        "workspace_ref": {"kind": "assistant", "assistant_id": assistant_id} if assistant_id else {"kind": "agent", "agent_id": agent_id},
        "session_kind": "assistant_chat" if assistant_id else "agent_chat",
        "artifact_grant": grant}
    return PlatformMCPService(
        sessions_repo=SimpleNamespace(get_session=AsyncMock(return_value=session)),
        binding_repo=SimpleNamespace(get_binding=AsyncMock(return_value=binding)),
        # The endpoint reads the origin Agent's Artifacts switch on every call;
        # this stored Agent leaves it at its default.
        agent_config=SimpleNamespace(get_agent_access_doc=AsyncMock(return_value={"agent_id": agent_id})),
        expose_port_service=None,
        artifact_service=service,
        tool_cards=SimpleNamespace(create=AsyncMock()),
    )


async def tool(mcp: PlatformMCPService, name: str, args: dict) -> dict:
    return await mcp.handle_jsonrpc(
        "session",
        "artifacts",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )


def result(response: dict) -> dict:
    assert "error" not in response, response
    text = json.loads(response["result"]["content"][0]["text"])
    assert text == response["result"]["structuredContent"]
    return text


@pytest.mark.asyncio
async def test_viewer_grant_reads_only_that_foreign_artifact_and_uses_viewer_db_rules(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    granted = await create_page(service, {"db": {}})
    other = await create_page(service, {"db": {}})
    share = await service.enable_share(OWNER, granted, access="interact")
    grant = {"artifact_id": granted, "source": "share", "token": share["token"]}
    mcp = mcp_service(
        service, user_id=VIEWER.user_id, grant=grant
    )
    assert result(await tool(mcp, "get_artifact", {"artifact_id": granted}))["content"] == "hello"
    refused = await tool(mcp, "get_artifact", {"artifact_id": other})
    assert "ARTIFACT_NOT_FOUND" in refused["error"]["message"]
    refused = await tool(mcp, "update_artifact", {"artifact_id": granted, "content": "hijack"})
    assert "ARTIFACT_NOT_WRITER" in refused["error"]["message"]
    granted_viewer = await service.resolve_viewer(
        user=VIEWER, artifact_id=granted, grant=grant
    )
    path = f"data/users/{granted_viewer.id}/profile"
    written = result(
        await tool(
            mcp,
            "artifact_data",
            {"artifact_id": granted, "action": "set", "path": path, "data": {"mine": True}},
        )
    )
    assert written["astrabox_artifact"]["id"] == granted
    assert result(
        await tool(mcp, "artifact_data", {"artifact_id": granted, "action": "get", "path": path})
    )["data"] == {"mine": True}
    owner_viewer = await service.resolve_viewer(user=OWNER, artifact_id=granted)
    refused = await tool(
        mcp,
        "artifact_data",
        {"artifact_id": granted, "action": "get", "path": f"data/users/{owner_viewer.id}/profile"},
    )
    assert "ARTIFACT_DATA_DENIED" in refused["error"]["message"]
    refused = await tool(
        mcp, "artifact_data", {"artifact_id": other, "action": "get", "path": "items/a"}
    )
    assert "ARTIFACT_NOT_FOUND" in refused["error"]["message"]
    listing = result(await tool(mcp, "list_artifacts", {}))
    assert [item["astrabox_artifact"]["id"] for item in listing["items"]] == [granted]
    assert set(listing["items"][0]) == {"astrabox_artifact", "url"}
    await service.enable_share(OWNER, granted, access="view")
    view_only = mcp_service(
        service, user_id=VIEWER.user_id, grant=grant
    )
    refused = await tool(
        view_only,
        "artifact_data",
        {"artifact_id": granted, "action": "set", "path": "items/a", "data": {}},
    )
    assert "ARTIFACT_DATA_DENIED" in refused["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "expire", "replace-token", "downgrade"])
async def test_share_grant_rechecks_admission_and_current_access(artifact_service, change) -> None:
    service = artifact_service
    aid = await create_page(service, {"db": {}})
    share = await service.enable_share(OWNER, aid, access="interact")
    # A stale access snapshot must confer no authority, even if present in the binding.
    grant = {"artifact_id": aid, "source": "share", "token": share["token"], "level": "interact"}
    mcp = mcp_service(service, user_id=VIEWER.user_id, grant=grant)
    viewer = await service.resolve_viewer(user=VIEWER, artifact_id=aid, grant=grant)
    result(await tool(mcp, "artifact_data", {"artifact_id": aid, "action": "set", "path": "items/a", "data": {}}))
    if change == "revoke":
        await service.disable_share(OWNER, aid)
    else:
        shares = ArtifactShare.__table__
        await execute(update(shares).where(shares.c.artifact_id == aid).values(**{
            "expire": {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
            "replace-token": {"token": "replacement"},
            "downgrade": {"access": "view"},
        }[change]))
    refused = await tool(mcp, "artifact_data", {"artifact_id": aid, "action": "set", "path": "items/b", "data": {}})
    assert ("ARTIFACT_DATA_DENIED" if change == "downgrade" else "ARTIFACT_SHARE_NOT_FOUND") in refused["error"]["message"]
    from astrabox.common.utils.errors import APIError
    with pytest.raises(APIError):
        await service.data_write(viewer, [{"action": "set", "path": "items/c", "data": {}}])
    owner = await service.resolve_viewer(user=OWNER, artifact_id=aid)
    assert not (await service.data_get(owner, "items/b"))["exists"]
    if change != "downgrade":
        assert "ARTIFACT_SHARE_NOT_FOUND" in (await tool(mcp, "get_artifact", {"artifact_id": aid}))["error"]["message"]


@pytest.mark.asyncio
async def test_share_grant_reads_only_the_currently_served_version(artifact_service) -> None:
    service = artifact_service
    aid = await create_page(service)
    await service.update(OWNER, aid, {"content": "private new content"})
    share = await service.enable_share(OWNER, aid, version=1, access="interact")
    mcp = mcp_service(service, user_id=VIEWER.user_id, grant={
        "artifact_id": aid, "source": "share", "token": share["token"], "level": "interact",
    })
    assert result(await tool(mcp, "get_artifact", {"artifact_id": aid}))["content"] == "hello"
    assert "ARTIFACT_NOT_FOUND" in (await tool(mcp, "get_artifact", {"artifact_id": aid, "version": 2}))["error"]["message"]
    await service.enable_share(OWNER, aid, version=None)
    assert result(await tool(mcp, "get_artifact", {"artifact_id": aid}))["content"] == "private new content"
    assert "ARTIFACT_NOT_FOUND" in (await tool(mcp, "get_artifact", {"artifact_id": aid, "version": 1}))["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_grant", [False, True])
async def test_grant_binding_cannot_reach_viewers_other_owned_artifacts_or_create(artifact_service, owner_grant) -> None:
    service = artifact_service
    aid = await create_page(service, {"db": {}})
    other = (await service.create(VIEWER, {"title": "Private", "kind": "html", "content": "secret", "capabilities": {"db": {}}}, origin={"agent_id": "agent"}))["artifact_id"]
    share = await service.enable_share(OWNER, aid, access="interact")
    user_id = OWNER.user_id if owner_grant else VIEWER.user_id
    if owner_grant:
        other = await create_page(service, {"db": {}})
    grant = {"artifact_id": aid, "source": "owner", "level": "owner"} if owner_grant else {
        "artifact_id": aid, "source": "share", "token": share["token"], "level": "interact",
    }
    mcp = mcp_service(service, user_id=user_id, grant=grant)
    assert [r["astrabox_artifact"]["id"] for r in result(await tool(mcp, "list_artifacts", {}))["items"]] == [aid]
    assert "ARTIFACT_NOT_WRITER" in (await tool(mcp, "create_artifact", {"title": "Copy", "kind": "html", "content": "copy"}))["error"]["message"]
    for name, extra in [("get_artifact", {}), ("update_artifact", {"content": "copy"}), ("artifact_data", {"action": "get", "path": "items/a"}), ("artifact_data", {"action": "set", "path": "items/a", "data": {}})]:
        assert "ARTIFACT_NOT_FOUND" in (await tool(mcp, name, {"artifact_id": other, **extra}))["error"]["message"]
    if owner_grant:
        result(await tool(mcp, "update_artifact", {"artifact_id": aid, "content": "owner edit"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("assistant", [False, True])
async def test_artifact_tools_cannot_cross_binding_agent_or_assistant_origin(artifact_service, assistant) -> None:
    service = artifact_service
    scope = {"assistant_id": "assistant-one"} if assistant else {"agent_id": "agent-1"}
    other_scope = {"assistant_id": "assistant-two"} if assistant else {"agent_id": "agent-two"}
    async def page(origin):
        return (await service.create(OWNER, {"title": "Dashboard", "kind": "html", "content": "private", "capabilities": {"db": {}}}, origin=origin))["artifact_id"]
    aid, foreign = await page(scope), await page(other_scope)
    cross_kind = await page({"agent_id": "agent-1"} if assistant else {"assistant_id": "assistant-one"})
    mcp = mcp_service(service, assistant_id="assistant-one" if assistant else None, agent_id=None if assistant else "agent-1")
    assert [r["astrabox_artifact"]["id"] for r in result(await tool(mcp, "list_artifacts", {}))["items"]] == [aid]
    assert result(await tool(mcp, "get_artifact", {"artifact_id": aid}))["content"] == "private"
    result(await tool(mcp, "update_artifact", {"artifact_id": aid, "content": "refreshed"}))
    result(await tool(mcp, "artifact_data", {"artifact_id": aid, "action": "set", "path": "items/a", "data": {"value": 1}}))
    assert result(await tool(mcp, "artifact_data", {"artifact_id": aid, "action": "get", "path": "items/a"}))["data"] == {"value": 1}
    created = result(await tool(mcp, "create_artifact", {"title": "New", "kind": "html", "content": "new"}))
    record = await service.records.get(created["astrabox_artifact"]["id"])
    assert all(record["origin"][key] == value for key, value in scope.items())
    for forbidden in (foreign, cross_kind):
        for name, extra in [("get_artifact", {}), ("update_artifact", {"content": "stolen"}), ("artifact_data", {"action": "get", "path": "items/a"}), ("artifact_data", {"action": "set", "path": "items/a", "data": {}})]:
            response = await tool(mcp, name, {"artifact_id": forbidden, **extra})
            assert "ARTIFACT_NOT_FOUND" in response["error"]["message"]


@pytest.mark.asyncio
async def test_channel_session_and_claimed_slot_cannot_discover_or_call_artifact_tools(artifact_service) -> None:
    mcp = mcp_service(artifact_service, source_type="channel")
    aid = await create_page(artifact_service)
    for deployment_id in ("session", "mcp_prepared-slot"):
        listing = await mcp.handle_jsonrpc(deployment_id, "artifacts", {"id": 1, "method": "tools/list"})
        assert listing["error"]["code"] == -32601
        response = await mcp.handle_jsonrpc(deployment_id, "artifacts", {
            "id": 1, "method": "tools/call", "params": {"name": "get_artifact", "arguments": {"artifact_id": aid}},
        })
        assert response["error"]["code"] == -32601
