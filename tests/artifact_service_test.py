"""Artifact authorization, and the races on its versions, budgets and viewers, on the
PostgreSQL store.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.config.settings import get_settings
from astrabox.core.service.artifacts.agent_runs import ArtifactAgentRuns
from astrabox.core.service.artifacts.sample import ArtifactSampleService
from astrabox.core.service.artifacts.service import ArtifactService, opaque_viewer_id, guest_viewer_id
from astrabox.persistence.repository.artifact_budget_repository import ArtifactBudgetRepository
from astrabox.persistence.repository.artifact_data_repository import ArtifactDataRepository
from astrabox.persistence.repository.artifact_event_repository import ArtifactEventRepository
from astrabox.persistence.repository.artifact_record_repository import ArtifactRecordRepository
from astrabox.persistence.repository.artifact_version_repository import ArtifactVersionRepository
from astrabox.persistence.repository.artifact_viewer_repository import ArtifactViewerRepository
from astrabox.persistence.sql import execute, fetch_all, fetch_value, transaction
from astrabox.persistence.sql.models import artifacts as tables

OWNER = UserContext("owner", display_name="Owner")
VIEWER = UserContext("viewer", display_name="Viewer")
ORIGIN = {"session_kind": "agent_chat", "agent_id": "agent-1", "session_id": "source"}

_EVENTS = tables.ArtifactEvent.__table__
_SHARES = tables.ArtifactShare.__table__
_LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def new_service() -> ArtifactService:
    return ArtifactService(
        records=ArtifactRecordRepository(),
        versions=ArtifactVersionRepository(),
        data=ArtifactDataRepository(),
        events=ArtifactEventRepository(),
        viewers=ArtifactViewerRepository(),
        budgets=ArtifactBudgetRepository(),
    )


@pytest_asyncio.fixture
async def artifact_service(database: str, monkeypatch: Any):
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "test-artifact-key")
    get_settings.cache_clear()
    yield new_service()


async def journal(artifact_id: str) -> list[dict[str, Any]]:
    """The artifact's journal rows as stored, in sequence order."""
    return await fetch_all(
        select(_EVENTS.c.seq, _EVENTS.c.kind, _EVENTS.c.payload)
        .where(_EVENTS.c.artifact_id == artifact_id)
        .order_by(_EVENTS.c.seq)
    )


async def create_page(service: ArtifactService, capabilities: dict | None = None) -> str:
    result = await service.create(
        OWNER,
        {"title": "Page", "kind": "html", "content": "hello", "capabilities": capabilities or {}},
        origin=ORIGIN,
    )
    return result["artifact_id"]


@pytest.mark.asyncio
async def test_concurrent_base_version_has_one_winner_and_one_journal_publish(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(service)
    results = await asyncio.gather(
        service.update(OWNER, aid, {"content": "A", "base_version": 1}),
        service.update(OWNER, aid, {"content": "B", "base_version": 1}),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    errors = [r for r in results if isinstance(r, APIError)]
    assert len(errors) == 1 and errors[0].code == "ARTIFACT_VERSION_CONFLICT"
    assert len(await service.list_versions(OWNER, aid)) == 2
    # The loser allocated no sequence: the winner's publish is the journal's second event.
    assert (await service.records.get(aid))["event_seq"] == 2
    assert [(e["seq"], e["payload"]["version"]) for e in await journal(aid)] == [(2, 2)]


@pytest.mark.asyncio
async def test_share_levels_anonymous_identity_and_delete_cascade(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(service, {"db": {}, "user": {}})
    share = await service.enable_share(OWNER, aid, access="interact")
    with pytest.raises(APIError, match="invalid guest_id"):
        await service.resolve_viewer(user=None, token=share["token"])
    signed = await service.resolve_viewer(user=VIEWER, token=share["token"])
    guest = await service.resolve_viewer(user=None, token=share["token"], guest_id="load-1")
    assert signed.level == "interact" and not signed.guest
    assert signed.id == opaque_viewer_id(aid, VIEWER.user_id)
    assert guest.level == "view" and guest.guest and guest.id == guest_viewer_id(share["token"], "load-1")
    assert (await service.resolve_viewer(user=OWNER, token=share["token"])).level == "owner"
    with pytest.raises(APIError):
        await service.data_write(guest, [{"action": "set", "path": "items/a", "data": {}}])
    await service.data_write(signed, [{"action": "set", "path": "items/a", "data": {"x": 1}}])
    await service.delete(OWNER, aid)
    for table in (
        tables.Artifact,
        tables.ArtifactVersion,
        tables.ArtifactVersionContent,
        tables.ArtifactShare,
        tables.ArtifactDocument,
        tables.ArtifactEvent,
        tables.ArtifactViewer,
    ):
        rows = table.__table__
        assert await fetch_value(
            select(func.count()).select_from(rows).where(rows.c.artifact_id == aid)
        ) == 0
    with pytest.raises(APIError) as error:
        await service.resolve_viewer(user=VIEWER, token=share["token"])
    assert error.value.code == "ARTIFACT_SHARE_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["sample", "agent"])
async def test_anyone_is_rejected_for_current_model_manifest_and_old_links_require_login(
    artifact_service: ArtifactService, capability: str
) -> None:
    service = artifact_service
    aid = await create_page(service)
    share = await service.enable_share(OWNER, aid)
    await service.update(OWNER, aid, {"content": "model page", "capabilities": {capability: {}}})
    with pytest.raises(APIError) as error:
        await service.enable_share(OWNER, aid, version=1)
    assert error.value.code == "ARTIFACT_SHARE_AUDIENCE_RESTRICTED"
    with pytest.raises(APIError) as error:
        await service.resolve_viewer(user=None, token=share["token"])
    assert error.value.code == "ARTIFACT_SIGN_IN_REQUIRED"
    signed = await service.resolve_viewer(user=VIEWER, token=share["token"])
    assert signed.level == "view"


@pytest.mark.asyncio
async def test_pinned_share_uses_current_manifest_and_owner_cannot_publish_it(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(service, {"artifact": {}, "db": {}})
    await service.update(OWNER, aid, {"content": "new page", "capabilities": {"sample": {}, "artifact": {}}})
    share = await service.enable_share(OWNER, aid, audience="signed_in", version=1)
    viewer = await service.resolve_viewer(user=OWNER, token=share["token"])
    runtime = await service.runtime(viewer)
    assert runtime["content"] == "hello" and runtime["version"] == 1
    assert runtime["manifest"] == {"sample": {}, "artifact": {}}
    with pytest.raises(APIError) as error:
        await service.publish(viewer, "rewrite", 1)
    assert error.value.code == "ARTIFACT_NOT_WRITER"
    with pytest.raises(APIError) as error:
        await service.resolve_viewer(user=None, token=share["token"])
    assert error.value.code == "ARTIFACT_SIGN_IN_REQUIRED"


@pytest.mark.asyncio
async def test_pinned_model_content_has_only_current_runtime_capabilities(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(service, {"agent": {}})
    await service.update(OWNER, aid, {"content": "static", "capabilities": {}})
    share = await service.enable_share(OWNER, aid, version=1)
    guest = await service.resolve_viewer(user=None, token=share["token"], guest_id="pinned-view")
    runtime = await service.runtime(guest)
    assert runtime["content"] == "hello" and runtime["manifest"] == {}
    await service.update(OWNER, aid, {"content": "model", "capabilities": {"sample": {}}})
    with pytest.raises(APIError) as error:
        await service.runtime(guest)
    assert error.value.code == "ARTIFACT_SIGN_IN_REQUIRED"


@pytest.mark.asyncio
async def test_expiry_is_fail_closed_and_revocation_invalidates_existing_viewers(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(service)
    share = await service.enable_share(OWNER, aid)
    viewer = await service.resolve_viewer(user=VIEWER, token=share["token"])
    await service.disable_share(OWNER, aid)
    with pytest.raises(APIError) as error:
        await service.runtime(viewer)
    assert error.value.code == "ARTIFACT_SHARE_NOT_FOUND"
    await service.enable_share(OWNER, aid)
    await execute(update(_SHARES).where(_SHARES.c.artifact_id == aid).values(expires_at=_LONG_AGO))
    with pytest.raises(APIError) as error:
        await service.resolve_viewer(user=None, token=share["token"])
    assert error.value.code == "ARTIFACT_SHARE_NOT_FOUND"


@pytest.mark.asyncio
async def test_private_data_queries_filter_before_limit_and_atomic_batch_rolls_back(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(
        service, {"db": {"rules": [{"path": "items/private", "read": "owner", "write": "owner"}]}}
    )
    owner = await service.resolve_viewer(user=OWNER, artifact_id=aid)
    await service.data_write(
        owner,
        [
            {"action": "set", "path": "items/private", "data": {"rank": 1}},
            {"action": "set", "path": "items/public", "data": {"rank": 2}},
        ],
    )
    share = await service.enable_share(OWNER, aid, access="interact")
    viewer = await service.resolve_viewer(user=VIEWER, token=share["token"])
    query = await service.data_query(
        viewer,
        "items",
        where=[{"field": "rank", "op": ">=", "value": 1}],
        order_by=[{"field": "rank", "direction": "asc"}],
        limit=1,
    )
    assert [row["id"] for row in query["items"]] == ["public"]
    with pytest.raises(APIError):
        await service.data_write(
            viewer,
            [
                {"action": "update", "path": "items/public", "data": {"rank": 99}},
                {"action": "set", "path": "items/private", "data": {"rank": 99}},
            ],
        )
    assert (await service.data_get(owner, "items/public"))["data"] == {"rank": 2}


@pytest.mark.asyncio
async def test_cursor_listing_owner_filters_literal_search_and_manifest_carry_forward(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(service, {"db": {}})
    other = await create_page(service)
    await service.patch(OWNER, aid, {"title": "Literal .* text"})
    page = await service.list(OWNER, limit=1)
    assert page["has_more"] and len(page["items"]) == 1
    second = await service.list(OWNER, limit=1, cursor=page["next_cursor"])
    assert not second["has_more"] and {
        r["artifact_id"] for r in page["items"] + second["items"]
    } == {aid, other}
    assert (await service.list(VIEWER))["items"] == []
    assert [r["artifact_id"] for r in (await service.list(OWNER, q=".*"))["items"]] == [aid]
    assert len((await service.list(OWNER, session_id="source"))["items"]) == 2
    assert (await service.list(OWNER, assistant_id="foreign"))["items"] == []
    await service.update(OWNER, aid, {"content": "same capabilities"})
    assert (await service.get(OWNER, aid))["manifest"] == {"db": {}}
    await service.update(OWNER, aid, {"content": "clear", "capabilities": {}})
    assert (await service.get(OWNER, aid))["manifest"] == {}


@pytest.mark.asyncio
async def test_pin_never_keeps_revoked_live_data_or_room_policy(artifact_service) -> None:
    service = artifact_service
    aid = await create_page(service, {"db": {}, "room": {"topics": {"update": "interact"}}})
    share = await service.enable_share(OWNER, aid, access="interact", version=1)
    viewer = await service.resolve_viewer(user=VIEWER, token=share["token"])
    await service.data_write(viewer, [{"action": "set", "path": "items/a", "data": {"secret": True}}])
    await service.room_write(viewer, "default", topic="update", data={})
    policy = {"db": {"rules": [{"path": "items", "read": "owner", "write": "owner"}]}, "room": {"topics": {"update": "owner"}}}
    await service.update(OWNER, aid, {"content": "new content", "capabilities": policy})
    runtime = await service.runtime(viewer)
    assert runtime["content"] == "hello" and runtime["version"] == 1
    assert runtime["manifest"] == policy
    with pytest.raises(APIError) as error:
        await service.data_get(viewer, "items/a")
    assert error.value.code == "ARTIFACT_DATA_DENIED"
    with pytest.raises(APIError) as error:
        await service.data_write(viewer, [{"action": "set", "path": "items/b", "data": {}}])
    assert error.value.code == "ARTIFACT_DATA_DENIED"
    with pytest.raises(APIError) as error:
        await service.room_write(viewer, "default", topic="update", data={})
    assert error.value.code == "ARTIFACT_ROOM_DENIED"
    await service.update(OWNER, aid, {"content": "disabled", "capabilities": {}})
    with pytest.raises(APIError) as error:
        await service.data_query(viewer, "items")
    assert error.value.code == "ARTIFACT_CAPABILITY_NOT_DECLARED"


@pytest.mark.asyncio
async def test_guest_presence_identity_is_secret_derived_and_share_scoped(artifact_service) -> None:
    service = artifact_service
    ids = []
    for _ in range(2):
        aid = await create_page(service, {"room": {}})
        share = await service.enable_share(OWNER, aid)
        guest = await service.resolve_viewer(user=None, token=share["token"], guest_id="raw-load")
        ids.append(guest.id)
        assert guest.id.startswith("guest_") and "raw-load" not in guest.id
        repeated = await service.resolve_viewer(user=None, token=share["token"], guest_id="raw-load")
        assert repeated.id == guest.id
        impostor = await service.resolve_viewer(user=None, token=share["token"], guest_id=guest.id.removeprefix("guest_"))
        assert impostor.id != guest.id
        await service.room_write(guest, "default", presence=True, data={"here": True})
        row = next(row for row in await journal(aid) if row["kind"] == "presence")
        assert row["payload"]["viewer_id"] == guest.id
        assert "raw-load" not in str(row)
    assert ids[0] != ids[1]


@pytest.mark.asyncio
async def test_guests_cannot_resolve_signed_in_profile_names(artifact_service) -> None:
    service = artifact_service
    aid = await create_page(service, {"user": {}})
    share = await service.enable_share(OWNER, aid)
    owner = await service.resolve_viewer(user=OWNER, artifact_id=aid)
    guest = await service.resolve_viewer(user=None, token=share["token"], guest_id="load")
    assert (await service.profiles(guest, [owner.id]))["profiles"] == [{"id": owner.id, "display_name": ""}]
    assert (await service.profiles(owner, [owner.id]))["profiles"][0]["display_name"] == "Owner"


@pytest.mark.asyncio
async def test_a_signed_in_viewer_is_registered_while_a_write_holds_the_artifact(
    artifact_service,
) -> None:
    # Every signed-in runtime request registers its viewer. It must not queue
    # behind a write to the artifact, which holds the artifact's lock.
    service = artifact_service
    aid = await create_page(service, {"user": {}})
    share = await service.enable_share(OWNER, aid)
    locked, release = asyncio.Event(), asyncio.Event()

    async def hold(conn: Any) -> None:
        await service.records.lock(aid, conn=conn)
        locked.set()
        await release.wait()

    holder = asyncio.ensure_future(transaction(hold))
    await locked.wait()
    try:
        viewer = await asyncio.wait_for(
            service.resolve_viewer(user=VIEWER, token=share["token"]), timeout=5
        )
    finally:
        release.set()
        await holder
    assert (await service.profiles(viewer, [viewer.id]))["profiles"] == [
        {"id": viewer.id, "display_name": "Viewer"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["room", "sample", "agent"])
async def test_a_viewers_first_requests_at_once_share_one_budget_without_a_collision(
    artifact_service, budget: str
) -> None:
    # Two requests of a viewer who has never used the budget arrive together,
    # on two artifacts. Both are within every limit and must both be admitted.
    service = artifact_service
    capabilities = {"room": {}, "sample": {}, "agent": {}}
    viewers = [
        await service.resolve_viewer(
            user=OWNER, artifact_id=await create_page(service, capabilities)
        )
        for _ in range(2)
    ]
    if budget == "room":
        requests = [service.room_write(v, "default", presence=True, data={}) for v in viewers]
    elif budget == "sample":
        sampling = ArtifactSampleService(artifacts=service, origin_access=SimpleNamespace())
        requests = [sampling._reserve(OWNER.user_id, uuid.uuid4().hex) for _ in viewers]
    else:
        runs = ArtifactAgentRuns(
            artifacts=service, origin_access=SimpleNamespace(), platform=SimpleNamespace(),
            agent_service_getter=None, assistant_service_getter=None,
        )
        requests = [runs._reserve(v, uuid.uuid4().hex) for v in viewers]
    outcomes = await asyncio.gather(*requests, return_exceptions=True)
    assert [o for o in outcomes if isinstance(o, BaseException)] == []
