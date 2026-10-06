"""SQLite document operations prove artifact atomicity and authorization decisions."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.config.settings import get_settings
from astrabox.core.service.artifacts import limits
from astrabox.core.service.artifacts.service import ArtifactService, opaque_viewer_id, guest_viewer_id
from astrabox.persistence.repository.artifact_data_repository import ArtifactDataRepository
from astrabox.persistence.repository.artifact_event_repository import ArtifactEventRepository
from astrabox.persistence.repository.artifact_record_repository import ArtifactRecordRepository
from astrabox.persistence.repository.artifact_version_repository import ArtifactVersionRepository
from astrabox.persistence.repository.artifact_viewer_repository import ArtifactViewerRepository
from astrabox.persistence.repository.sqlite.engine import dispose_engines

OWNER = UserContext("owner", display_name="Owner")
VIEWER = UserContext("viewer", display_name="Viewer")
ORIGIN = {"session_kind": "agent_chat", "agent_id": "agent-1", "session_id": "source"}


@pytest_asyncio.fixture
async def artifact_service(tmp_path: Any, monkeypatch: Any):
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "test-artifact-key")
    get_settings.cache_clear()
    service = ArtifactService(
        records=ArtifactRecordRepository(),
        versions=ArtifactVersionRepository(),
        data=ArtifactDataRepository(),
        events=ArtifactEventRepository(),
        viewers=ArtifactViewerRepository(),
    )
    try:
        yield service
    finally:
        await dispose_engines()
        get_settings.cache_clear()


async def create_page(service: ArtifactService, capabilities: dict | None = None) -> str:
    result = await service.create(
        OWNER,
        {"title": "Page", "kind": "html", "content": "hello", "capabilities": capabilities or {}},
        origin=ORIGIN,
    )
    return result["artifact_id"]


@pytest.mark.asyncio
async def test_create_update_restore_and_edit_failure_are_atomic(
    artifact_service: ArtifactService,
) -> None:
    service = artifact_service
    aid = await create_page(service, {"artifact": {}})
    await service.update(
        OWNER, aid, {"edits": [{"old_string": "hello", "new_string": "world"}], "base_version": 1}
    )
    assert (await service.get(OWNER, aid))["content"] == "world"
    assert (await service.get(OWNER, aid, 1))["content"] == "hello"
    with pytest.raises(APIError, match="absent or ambiguous"):
        await service.update(
            OWNER,
            aid,
            {
                "edits": [
                    {"old_string": "world", "new_string": "x"},
                    {"old_string": "absent", "new_string": "y"},
                ]
            },
        )
    assert len(await service.list_versions(OWNER, aid)) == 2
    restored = await service.restore(OWNER, aid, 1)
    assert restored["current_version"] == 3
    assert (await service.get(OWNER, aid))["content"] == "hello"
    assert restored["manifest"] == {"artifact": {}}


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
    events = await service.events.list({"artifact_id": aid}, sort=[("seq", 1)])
    assert [e["seq"] for e in events] == [1, 2]
    assert [e["payload"]["version"] for e in events] == [1, 2]


@pytest.mark.asyncio
async def test_limits_do_not_leave_a_version_or_data_event(
    artifact_service: ArtifactService, monkeypatch: Any
) -> None:
    service = artifact_service
    aid = await create_page(service, {"db": {}})
    monkeypatch.setattr(limits, "CONTENT_BYTES", 5)
    with pytest.raises(APIError) as error:
        await service.update(OWNER, aid, {"content": "too large"})
    assert error.value.code == "ARTIFACT_CONTENT_TOO_LARGE"
    monkeypatch.setattr(limits, "VERSION_COUNT", 1)
    with pytest.raises(APIError) as error:
        await service.update(OWNER, aid, {"content": "ok"})
    assert error.value.code == "ARTIFACT_VERSION_LIMIT"
    viewer = await service.resolve_viewer(user=OWNER, artifact_id=aid)
    monkeypatch.setattr(limits, "DOCUMENT_COUNT", 1)
    with pytest.raises(APIError) as error:
        await service.data_write(
            viewer,
            [
                {"action": "set", "path": "items/a", "data": {}},
                {"action": "set", "path": "items/b", "data": {}},
            ],
        )
    assert error.value.code == "ARTIFACT_DATA_COUNT_LIMIT"
    assert not (await service.data_get(viewer, "items/a"))["exists"]
    await service.data_write(viewer, [{"action": "set", "path": "items/a", "data": {}}])
    with pytest.raises(APIError) as error:
        await service.data_write(viewer, [{"action": "set", "path": "items/b", "data": {}}])
    assert error.value.code == "ARTIFACT_DATA_COUNT_LIMIT"
    assert not (await service.data_get(viewer, "items/b"))["exists"]
    assert len(await service.events.list({"artifact_id": aid})) == 2


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
    for repo in service._repos:
        assert await repo.list({"artifact_id": aid}) == []
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
    row = await service.records.get(aid)
    row["share"]["expires_at"] = "invalid-time"
    await service.records.put(row)
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
async def test_title_and_document_byte_limits(
    artifact_service: ArtifactService, monkeypatch: Any
) -> None:
    service = artifact_service
    aid = await create_page(service, {"db": {}})
    with pytest.raises(APIError) as error:
        await service.patch(OWNER, aid, {"title": "x" * (limits.TITLE_CHARACTERS + 1)})
    assert error.value.code == "ARTIFACT_TITLE_LIMIT"
    viewer = await service.resolve_viewer(user=OWNER, artifact_id=aid)
    monkeypatch.setattr(limits, "DOCUMENT_BYTES", 5)
    with pytest.raises(APIError) as error:
        await service.data_write(
            viewer, [{"action": "set", "path": "items/a", "data": {"value": "large"}}]
        )
    assert error.value.code == "ARTIFACT_DATA_LIMIT"
    assert not (await service.data_get(viewer, "items/a"))["exists"]


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
        row = (await service.events.list({"artifact_id": aid, "type": "presence"}))[0]
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
@pytest.mark.parametrize("guest", [True, False])
async def test_room_emit_and_presence_share_a_viewer_token_bucket(artifact_service, monkeypatch, guest) -> None:
    from astrabox.core.service.artifacts import service as module
    service = artifact_service
    aid = await create_page(service, {"room": {"topics": {"message": "view"}}})
    share = await service.enable_share(OWNER, aid)
    viewer = await service.resolve_viewer(user=None if guest else OWNER, token=share["token"], guest_id="load")
    replica = ArtifactService(records=service.records, versions=service.versions, data=service.data, events=service.events, viewers=service.viewers)
    now = module.utcnow()
    monkeypatch.setattr(module, "utcnow", lambda: now)
    for index in range(20):
        await (service if index % 2 else replica).room_write(viewer, "default", topic="message", presence=index % 2 == 0, data={})
    count = len(await service.events.list({"artifact_id": aid}))
    with pytest.raises(APIError) as error:
        await service.room_write(viewer, "other-room", presence=True, data={})
    assert error.value.code == "ARTIFACT_LIVE_RATE_LIMITED"
    assert len(await service.events.list({"artifact_id": aid})) == count
    now += timedelta(seconds=0.1)
    await service.room_write(viewer, "default", presence=True, data={})
    with pytest.raises(APIError):
        await service.room_write(viewer, "default", topic="message", data={})


@pytest.mark.asyncio
async def test_live_stream_cap_releases_admission_on_close_and_error(artifact_service) -> None:
    service = artifact_service
    aid = await create_page(service)
    viewer = await service.resolve_viewer(user=OWNER, artifact_id=aid)
    streams = [service.live(viewer) for _ in range(8)]
    try:
        for stream in streams:
            assert "event: version" in await anext(stream)
        other = await service.resolve_viewer(user=OWNER, artifact_id=await create_page(service))
        blocked = service.live(other)
        with pytest.raises(APIError) as error:
            await anext(blocked)
        assert error.value.code == "ARTIFACT_LIVE_RATE_LIMITED"
        await streams.pop().aclose()
        replacement = service.live(viewer)
        streams.append(replacement)
        assert "event: version" in await anext(replacement)
    finally:
        for stream in streams:
            await stream.aclose()
    share = await service.enable_share(OWNER, aid)
    shared = await service.resolve_viewer(user=VIEWER, token=share["token"])
    await service.disable_share(OWNER, aid)
    with pytest.raises(APIError):
        await anext(service.live(shared))
    assert service._live_counts == {}
    assert not service._broker._listeners
