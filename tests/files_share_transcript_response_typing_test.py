"""The typed file, share, transcript, callback and identity responses.

``response_model`` FILTERS the dict a handler returns: a field the model does
not declare is deleted from the response, and a field the model declares but the
handler omitted arrives as an explicit ``null``. Both are silent breaks — for
the console reading ``.data``, and for the in-box SessionStore adapter, whose
resume path reads ``entries``/``store_sequence`` off these same bodies.

Each case therefore compares the WHOLE body against the envelope the service
produced, so the assertion fails on a dropped key and on an added one. The
stubbed payloads carry the shapes a model can get wrong: an upload entry with no
``size`` (the field a listing has and an upload does not), a load answering
``entries: null`` (the SessionStore contract for an unknown key, which is not an
empty list), a callback arm whose sibling arm's fields must stay absent rather
than null, and a session projection whose fields no model here declares.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astrabox.api.routes import sandbox_callback as sandbox_callback_routes
from astrabox.api.routes import session_files as session_file_routes
from astrabox.api.routes import share as share_routes
from astrabox.api.routes import transcript as transcript_routes
from astrabox.api.routes import user as user_routes
from astrabox.common.utils.user_context import (
    UserContext,
    reset_current_user_context,
    set_current_user_context,
)
from astrabox.core.service.orchestrator.transcript_capability import (
    mint_sandbox_box_capability_token,
    mint_transcript_capability_token,
)
from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)

_SIGNING_KEY = "response-typing-test-signing-key"
_PLATFORM_SESSION = "session-1"
_SANDBOX_ID = "sbx-1"

# ── payloads the stubbed services return ────────────────────────────────────
#
# A listing entry carries `size` and `modified_at`; an upload entry carries
# neither, so a model that declares them as required — or a response that fills
# them with null — fails the comparison below. `provisioned_by` is declared by no
# model, so a filtered field fails it too.

_LISTED_FILE: dict[str, Any] = {
    "path": "/workspace/report.md",
    "name": "report.md",
    "kind": "file",
    "size": 12,
    "modified_at": "2026-08-06T00:00:00+00:00",
}

_LISTED_DIRECTORY: dict[str, Any] = {
    "path": "/workspace/data",
    "name": "data",
    "kind": "directory",
    "size": 0,
    "modified_at": "2026-08-06T00:00:00+00:00",
}

_LISTING: dict[str, Any] = {
    "root_path": "/workspace",
    "current_path": "/workspace",
    "parent_path": None,
    "entries": [_LISTED_DIRECTORY, _LISTED_FILE],
    "session_kind": "chat",
}

_UPLOAD_RESULT: dict[str, Any] = {
    "root_path": "/workspace",
    "current_path": "/workspace",
    "parent_path": None,
    "entries": [{"path": "/workspace/new.txt", "name": "new.txt", "kind": "file"}],
    "uploaded_count": 1,
}

_MKDIR_RESULT: dict[str, Any] = {
    "root_path": "/workspace",
    "current_path": "/workspace/data",
    "parent_path": "/workspace",
    "path": "/workspace/data",
}

_MOVE_RESULT: dict[str, Any] = {
    "root_path": "/workspace",
    "src_path": "/workspace/a.txt",
    "dest_path": "/workspace/data/a.txt",
}

_DELETE_RESULT: dict[str, Any] = {
    "root_path": "/workspace",
    "paths": ["/workspace/a.txt", "/workspace/gone.txt"],
    "deleted_count": 1,
}

_SHARE_LINK: dict[str, Any] = {
    "enabled": True,
    "token": "share-token-1",
    "expires_at": None,
    "allow_download": True,
    "created_at": "2026-08-01T00:00:00Z",
}

# The share viewer gets a purpose-built transcript projection, not the owner's
# Session/runtime document.
_SHARED_SESSION: dict[str, Any] = {
    "title": "A shared conversation",
    "delivery_state": "RECEIVED",
    "delivery_failure": None,
    "pending_interaction": None,
    "share_allow_download": True,
}

_SHARED_MESSAGES: dict[str, Any] = {
    "messages": [{"message_id": "m1", "role": "user", "content": "hello"}],
    "has_more": True,
    "active_turn_overlay": None,
    "pending_interaction": None,
}


def _envelope(data: Any) -> dict[str, Any]:
    return {"code": "OK", "message": "success", "data": data}


def _returns(payload: Any) -> Any:
    async def _call(*args: Any, **kwargs: Any) -> Any:
        return payload

    return _call


class _PlatformService:
    """The session-file and share calls the two route modules make."""

    list_session_files = _returns(_LISTING)
    upload_session_files = _returns(_UPLOAD_RESULT)
    create_session_directory = _returns(_MKDIR_RESULT)
    move_session_file = _returns(_MOVE_RESULT)
    delete_session_files = _returns(_DELETE_RESULT)
    create_session_share = _returns(_SHARE_LINK)
    get_session_share = _returns(_SHARE_LINK)
    revoke_session_share = _returns({"enabled": False})
    get_shared_session = _returns(_SHARED_SESSION)
    get_shared_messages = _returns(_SHARED_MESSAGES)
    list_shared_files = _returns(_LISTING)


@pytest.fixture
def file_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(session_file_routes, "_registered_on", None)
    monkeypatch.setattr(
        session_file_routes, "get_platform_service", lambda: _PlatformService()
    )
    app = FastAPI()
    session_file_routes.register_session_file_routes(app)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def share_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(share_routes, "_registered_on", None)
    monkeypatch.setattr(share_routes, "get_platform_service", lambda: _PlatformService())
    app = FastAPI()
    share_routes.register_share_routes(app)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def transcript_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", _SIGNING_KEY)
    monkeypatch.setattr(transcript_routes, "_registered_on", None)
    monkeypatch.setattr(TranscriptEntryRepository, "append_entries", _returns(3))
    monkeypatch.setattr(TranscriptEntryRepository, "load_entries", _returns(None))
    monkeypatch.setattr(TranscriptEntryRepository, "current_sequence", _returns(3))
    monkeypatch.setattr(
        TranscriptEntryRepository,
        "list_sessions",
        _returns([{"session_id": "sdk-session", "mtime": 42}]),
    )
    monkeypatch.setattr(
        TranscriptEntryRepository, "list_subkeys", _returns(["subagents/agent-child"])
    )
    app = FastAPI()
    transcript_routes.register_transcript_routes(app)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def callback_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", _SIGNING_KEY)
    monkeypatch.setattr(sandbox_callback_routes, "_registered_on", None)
    lifecycle = SimpleNamespace(
        handle_callback=_returns({"handled": False, "ignored": "stale_callback"}),
        converge_dead_sandbox_owners=_returns(
            SimpleNamespace(
                converged_sessions=("session-live",),
                ignored_sessions={"session-parked": "session_parked"},
                converged_agents=(),
                ignored_agents={},
                converged_assistant_workspaces=(),
                ignored_assistant_workspaces={},
            )
        ),
    )
    monkeypatch.setattr(
        sandbox_callback_routes,
        "get_platform_service",
        lambda: SimpleNamespace(_sandbox_lifecycle_service=lifecycle),
    )
    app = FastAPI()
    sandbox_callback_routes.register_sandbox_callback_routes(app)
    with TestClient(app) as client:
        yield client

@pytest.fixture
def user_client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(user_routes.router)
    token = set_current_user_context(
        UserContext("admin-1", display_name="Admin One", roles=["admin"])
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        reset_current_user_context(token)


def _capability_base(session_id: str = _PLATFORM_SESSION) -> str:
    token = mint_transcript_capability_token(session_id)
    return f"/api/v1/sbxcap/{token}/api/v1/transcript/{session_id}"


# ── the payload reaches the caller unchanged ────────────────────────────────


def test_a_file_listing_carries_every_entry_field(file_client: TestClient) -> None:
    response = file_client.post("/api/v1/sessions/session-1/files/list", json={})

    assert response.status_code == 200
    assert response.json() == _envelope(_LISTING)


def test_an_upload_entry_gains_no_size_it_never_measured(
    file_client: TestClient,
) -> None:
    """`size: null` on an uploaded file would read as an empty file."""
    response = file_client.post(
        "/api/v1/sessions/session-1/files/upload",
        files={"files": ("new.txt", b"body", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json() == _envelope(_UPLOAD_RESULT)
    assert set(response.json()["data"]["entries"][0]) == {"path", "name", "kind"}


def test_the_mutating_file_routes_answer_their_own_shapes(
    file_client: TestClient,
) -> None:
    made = file_client.post(
        "/api/v1/sessions/session-1/files/mkdir", json={"path": "data"}
    )
    moved = file_client.post(
        "/api/v1/sessions/session-1/files/move",
        json={"src_path": "a.txt", "dest_path": "data/a.txt"},
    )
    deleted = file_client.post(
        "/api/v1/sessions/session-1/files/delete",
        json={"paths": ["a.txt", "gone.txt"]},
    )

    assert made.json() == _envelope(_MKDIR_RESULT)
    assert moved.json() == _envelope(_MOVE_RESULT)
    assert deleted.json() == _envelope(_DELETE_RESULT)


def test_the_share_config_routes_answer_the_link_and_its_revocation(
    share_client: TestClient,
) -> None:
    """Revocation answers `enabled` alone — a null token would read as a live link."""
    created = share_client.post("/api/v1/sessions/session-1/share", json={})
    fetched = share_client.get("/api/v1/sessions/session-1/share")
    revoked = share_client.delete("/api/v1/sessions/session-1/share")

    assert created.json() == _envelope(_SHARE_LINK)
    assert fetched.json() == _envelope(_SHARE_LINK)
    assert revoked.json() == _envelope({"enabled": False})
    assert set(revoked.json()["data"]) == {"enabled"}


def test_a_shared_session_carries_only_the_transcript_projection(
    share_client: TestClient,
) -> None:
    response = share_client.get("/api/v1/share/share-token-1")

    assert response.status_code == 200
    assert response.json() == _envelope(_SHARED_SESSION)


def test_a_shared_message_page_keeps_only_viewer_fields(
    share_client: TestClient,
) -> None:
    response = share_client.get("/api/v1/share/share-token-1/messages?limit=20")

    assert response.status_code == 200
    assert response.json() == _envelope(_SHARED_MESSAGES)


def test_the_shared_file_listing_is_the_owner_listing(share_client: TestClient) -> None:
    response = share_client.get("/api/v1/share/share-token-1/files/list")

    assert response.status_code == 200
    assert response.json() == _envelope(_LISTING)


def test_an_append_answers_the_sequence_the_sender_records(
    transcript_client: TestClient,
) -> None:
    key = {"project_key": "project", "session_id": "sdk-session"}
    entries = [{"type": "assistant", "uuid": "e1"}]
    response = transcript_client.post(
        f"{_capability_base()}/append",
        json={
            "key": key,
            "entries": entries,
            "append_id": "batch-1",
            "payload_sha256": transcript_routes.transcript_payload_digest(key, entries),
        },
    )

    assert response.status_code == 200
    assert response.json() == _envelope({"ok": True, "count": 1, "store_sequence": 3})


def test_a_load_of_an_unknown_key_answers_null_entries(
    transcript_client: TestClient,
) -> None:
    """`entries: null` is the SessionStore contract for an unwritten key.

    Dropping the key, or turning it into `[]`, tells the in-box adapter the
    transcript exists and is empty — a resumed session would start blank.
    """
    response = transcript_client.post(
        f"{_capability_base()}/load",
        json={"key": {"project_key": "project", "session_id": "sdk-session"}},
    )

    assert response.status_code == 200
    assert response.json() == _envelope({"entries": None, "store_sequence": 3})


def test_the_enumeration_routes_answer_their_lists(
    transcript_client: TestClient,
) -> None:
    sessions = transcript_client.post(
        f"{_capability_base()}/list-sessions", json={"project_key": "project"}
    )
    subkeys = transcript_client.post(
        f"{_capability_base()}/list-subkeys",
        json={"key": {"project_key": "project", "session_id": "sdk-session"}},
    )

    assert sessions.json() == _envelope(
        {"sessions": [{"session_id": "sdk-session", "mtime": 42}]}
    )
    assert subkeys.json() == _envelope({"subkeys": ["subagents/agent-child"]})


def test_a_refused_transcript_token_still_answers_the_error_envelope(
    transcript_client: TestClient,
) -> None:
    """The rejection is a Response object, so the model must not reshape it."""
    response = transcript_client.post(
        f"/api/v1/sbxcap/not-a-token/api/v1/transcript/{_PLATFORM_SESSION}/load",
        json={"key": {"project_key": "project", "session_id": "sdk-session"}},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "FORBIDDEN"
    assert body["error"]["code"] == "FORBIDDEN"


def test_an_ignored_callback_names_only_why_it_was_ignored(
    callback_client: TestClient,
) -> None:
    """The applied arm's fields must stay absent, not arrive as null."""
    response = callback_client.post(
        "/api/v1/sandbox-callback/session/session-1/gen-1/token-1",
        json={"status": "TERMINATED"},
    )

    assert response.status_code == 200
    assert response.json() == _envelope({"handled": False, "ignored": "stale_callback"})


def test_a_terminating_notice_reports_both_lists(callback_client: TestClient) -> None:
    token = mint_sandbox_box_capability_token(_SANDBOX_ID)
    response = callback_client.post(
        f"/api/v1/sbxcap/{token}/api/v1/sandbox/{_SANDBOX_ID}/terminating",
        json={"reason": "preStop"},
    )

    assert response.status_code == 200
    assert response.json() == _envelope(
        {
            "handled": True,
            "converged": ["session-live"],
            "ignored": {"session-parked": "session_parked"},
        }
    )


def test_the_current_user_keeps_the_fields_an_idp_did_not_fill(
    user_client: TestClient,
) -> None:
    """`email` and `avatar_url` are null here; the console reads their presence."""
    response = user_client.get("/api/v1/user/current")

    assert response.status_code == 200
    assert response.json() == _envelope(
        {
            "user_id": "admin-1",
            "display_name": "Admin One",
            "email": None,
            "avatar_url": None,
            "roles": ["admin"],
            "is_admin": True,
        }
    )
