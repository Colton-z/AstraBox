"""The typed session / turn / Assistant responses document the wire without changing it.

``response_model`` FILTERS the dict a handler returns: a field the model does
not declare is deleted from the response, and a field the model declares but
the handler omitted arrives as an explicit ``null``. Both are silent breaks for
a console that reads ``.data``, and neither shows up in a route test that only
checks the status code.

Each case here therefore compares the WHOLE body against the envelope the
service produced, so the assertion fails on a dropped key and on an added one.
The stubbed payloads carry the three shapes a model can get wrong: a field no
model declares, a declared optional the payload omits, and a declared optional
the payload sets to ``null`` — the last one has to survive, because the session
renderer states "no turn is running" by writing ``current_turn_id: None`` and a
caller that stops receiving the key cannot tell that apart from a stale read.

The last group pins the documentation itself: a schema on the JSON routes, and
NO JSON body schema on the four streaming ones. A ``StreamingResponse`` route
bypasses ``response_model`` entirely, so a model there would not be validated
against anything — it would only publish a body the route never sends.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astrabox.api.routes import assistant as assistant_routes
from astrabox.api.routes import sessions as session_routes
from astrabox.api.routes import turns as turn_routes
from astrabox.common.utils.user_context import UserContext

_USER = UserContext(user_id="user-1", org_id="org-1")


# ── payloads the stubbed services return ────────────────────────────────────
#
# `_SESSION` is the closed public detail read. The many optional members of
# `SessionRecord` are absent, so a response that injects any of them as `null`
# still fails the whole-body comparison.

_SESSION: dict[str, Any] = {
    "session_id": "session-1",
    "user_id": "user-1",
    "state": "READY",
    "session_kind": "agent_chat",
    "agent_id": "agent-1",
    "current_turn_id": None,
    "last_turn_id": "turn-9",
    "agent_runtime": {
        "agent_id": "agent-1",
        "state": "ACTIVE",
        "sandbox_id": "sandbox-1",
        "runtime_unavailable": False,
    },
    "background_task_state": {
        "state": "OPEN",
        "pending_manifest_count": 1,
        "pending_task_count": 2,
        "opened_event_seq": 41,
        "source_turn_id": "turn-9",
    },
    "delivery_failure": None,
    "pending_interaction": {
        "interaction_id": "int-1",
        "session_id": "session-1",
        "turn_id": "turn-9",
        "tool_call_id": "tool-1",
        "tool_name": "Bash",
        "presentation": "tool_approval",
        "prompt": "Allow Bash?",
        "raw_input": {"command": "pwd"},
    },
    "engine_capabilities": {"tools": ["Bash"], "extra": {"vendor_flag": True}},
}

_SESSION_SUMMARY: dict[str, Any] = {
    "session_id": "session-2",
    "state": "PROCESSING",
    "title": "A conversation",
    "runtime_warning": False,
}

_SESSION_PAGE: dict[str, Any] = {
    "sessions": [_SESSION_SUMMARY],
    "has_more": True,
    "next_cursor": "cursor-2",
}

_MESSAGE_PAGE: dict[str, Any] = {
    "messages": [
        {
            "message_id": "msg-1",
            "role": "assistant",
            # Content blocks are engine vocabulary and are carried whole.
            "blocks": [{"type": "text", "text": "hello"}, {"type": "thinking"}],
        }
    ],
    "has_more": False,
    "active_turn_overlay": {"turn_id": "turn-9", "resume_cursor": {"frame_seq": 12}},
    "session_frame_seq": 12,
    "pending_interaction": None,
}

_CHILD_RUN_PAGE: dict[str, Any] = {
    "session_id": "session-1",
    "child_runs": [
        {
            "child_run_id": "child-1",
            "engine_kind": "claude_code",
            "depth": 1,
            "engine_event": "task_notification",
            "engine_status": "completed",
            "closed": True,
            "active": False,
            "operations": [],
            "summary": "Done",
        }
    ],
}

_CHILD_RUN_MESSAGES: dict[str, Any] = {
    "session_id": "session-1",
    "child_run_id": "child-1",
    "messages": [
        {
            "role": "assistant",
            "message_id": "message-1",
            "content": [{"type": "text", "text": "child answer"}],
        }
    ],
}

_PERMISSION_MODE: dict[str, Any] = {
    "session_id": "session-1",
    "permission_mode": "acceptEdits",
    "applied": True,
    "runtime_applied": True,
    "changed": True,
    "event_seq": 7,
}

_TERMINATION: dict[str, Any] = {
    "session_id": "session-1",
    "sandbox_id": "sandbox-1",
    "status": "sandbox-reclaimed",
    "killed": True,
}

_DELETION: dict[str, Any] = {"session_id": "session-1", "deleted": True}

_ARCHIVAL: dict[str, Any] = {
    "session_id": "session-1",
    "sandbox_id": "sandbox-1",
    "status": "conversation-archived",
    "killed": False,
    "archived": True,
}

_WEBSHELL: dict[str, Any] = {
    "session_id": "session-1",
    "sandbox_id": "sandbox-1",
    "url": "https://box.example.com/shell?token=opaque",
}

_TURN_RECEIPT: dict[str, Any] = {
    "turn_id": "turn-10",
    "command_id": "cmd-10",
    # A caller that sent no key of its own gets an explicit null back.
    "client_message_id": None,
    "accepted": True,
}

_INTERACTION_RECEIPT: dict[str, Any] = {
    "interaction_id": "int-1",
    "answered": True,
    "turn_id": "turn-9",
}

_INTERRUPT: dict[str, Any] = {"session_id": "session-1", "status": "accepted"}

_CHILD_RUN_STOP: dict[str, Any] = {
    "session_id": "session-1",
    "child_run_id": "child-1",
    "status": "accepted",
}

_CONVERSATION_END: dict[str, Any] = {
    "session_id": "session-1",
    "sandbox_id": "sandbox-1",
    "status": "conversation-ended",
    "killed": False,
}

# The catalog row is returned whole minus its private keys, so `owner_note` —
# declared by no model — must still reach the caller.
_ASSISTANT: dict[str, Any] = {
    "assistant_id": "asst_1",
    "owner_id": "user-1",
    "display_name": "Research",
    "icon": None,
    "description": None,
    "engine_kind": "assistant",
    "environment_name": "default",
    "permission_mode_default": "default",
    "model_config_override": {"model_name": "a-model"},
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-01T00:00:00Z",
    "owner_note": "seeded",
}

_ASSISTANT_DETAIL: dict[str, Any] = {
    **_ASSISTANT,
    "workspace_state": "READY",
    "current_sandbox_id": "sandbox-9",
}

_ASSISTANT_DELETION: dict[str, Any] = {"assistant_id": "asst_1", "deleted": True}

_WORKSPACE_WAKE: dict[str, Any] = {
    "assistant_id": "asst_1",
    "state": "MATERIALIZING",
    "engine_kind": "assistant",
    "provisioning_session_id": "session-boot-1",
}

_WORKSPACE_HIBERNATION: dict[str, Any] = {
    "assistant_id": "asst_1",
    "hibernated": True,
    "released": True,
    "recovery_required": False,
    "previous_sandbox_id": "sandbox-9",
    "sandbox_id": None,
    "hibernated_at": "2026-08-02T00:00:00Z",
}

_WORKSPACE_DESTRUCTION: dict[str, Any] = {"assistant_id": "asst_1", "destroyed": True}

_STARTED_CONVERSATION: dict[str, Any] = {
    "session_id": "session-3",
    "session_kind": "assistant_chat",
    "state": "CREATING",
    "workspace_note": "assistant workspace sandbox-9",
}


def _envelope(data: Any) -> dict[str, Any]:
    return {"code": "OK", "message": "success", "data": data}


def _returns(payload: Any) -> Any:
    async def _call(*args: Any, **kwargs: Any) -> Any:
        return payload

    return _call


async def _current_user(_request: Any = None) -> UserContext:
    return _USER


class _PlatformService:
    """The platform calls these routes make, answering the shapes above."""

    list_sessions = staticmethod(_returns([_SESSION]))
    list_sessions_page = staticmethod(_returns(_SESSION_PAGE))
    get_session = staticmethod(_returns(_SESSION))
    get_messages = staticmethod(_returns(_MESSAGE_PAGE))
    list_session_child_runs = staticmethod(_returns(_CHILD_RUN_PAGE))
    get_session_child_run_messages = staticmethod(_returns(_CHILD_RUN_MESSAGES))
    update_session_permission_mode = staticmethod(_returns(_PERMISSION_MODE))
    terminate_sandbox = staticmethod(_returns(_TERMINATION))
    recover_session = staticmethod(_returns(_SESSION))
    get_webshell_url = staticmethod(_returns(_WEBSHELL))
    delete_session = staticmethod(_returns(_DELETION))
    archive_session = staticmethod(_returns(_ARCHIVAL))
    must_own_session = staticmethod(_returns(_SESSION))
    dispatch_turn_input = staticmethod(_returns(_TURN_RECEIPT))
    answer_pending_interaction = staticmethod(_returns(_INTERACTION_RECEIPT))
    interrupt = staticmethod(_returns(_INTERRUPT))
    stop_session_child_run = staticmethod(_returns(_CHILD_RUN_STOP))
    end_conversation = staticmethod(_returns(_CONVERSATION_END))


class _AssistantService:
    create_assistant = staticmethod(_returns(_ASSISTANT))
    list_assistants = staticmethod(_returns([_ASSISTANT]))
    get_assistant = staticmethod(_returns(_ASSISTANT_DETAIL))
    update_assistant = staticmethod(_returns(_ASSISTANT))
    delete_assistant = staticmethod(_returns(_ASSISTANT_DELETION))
    wake_workspace = staticmethod(_returns(_WORKSPACE_WAKE))
    hibernate_workspace = staticmethod(_returns(_WORKSPACE_HIBERNATION))
    destroy_workspace = staticmethod(_returns(_WORKSPACE_DESTRUCTION))
    start_conversation = staticmethod(_returns(_STARTED_CONVERSATION))


# ── clients ─────────────────────────────────────────────────────────────────


# Each app is built once for the whole session, and the stubs are applied per
# test on top of it. The route modules that register onto an app skip the work
# when their module-level ``_registered_on`` still equals ``id(app)``
# (``astrabox/api/routes/assistant.py`` and fifteen siblings), and CPython
# reuses the address of a freed object — so a file that builds and drops an app
# per parameter case can leave an address behind that a later ``create_app()``
# lands on, and that app silently loses whole route families. Holding these two
# apps for the session keeps their addresses out of circulation.
#
# The stubs stay function-scoped because the handlers resolve ``_svc``,
# ``_resolve_user`` and ``get_current_user_context`` as module globals per
# request; only ``get_assistant_service`` is read once, at registration.


@pytest.fixture(scope="session")
def session_app() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(session_routes.router)
    app.include_router(turn_routes.router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def session_client(
    session_app: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    service = _PlatformService()
    for module in (session_routes, turn_routes):
        monkeypatch.setattr(module, "_svc", lambda service=service: service)
        monkeypatch.setattr(module, "_resolve_user", _current_user)
    return session_app


@pytest.fixture(scope="session")
def assistant_app() -> Iterator[TestClient]:
    app = FastAPI()
    with pytest.MonkeyPatch.context() as registration:
        registration.setattr(assistant_routes, "_registered_on", None)
        registration.setattr(assistant_routes, "get_assistant_service", _AssistantService)
        assistant_routes.register_assistant_routes(app)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def assistant_client(
    assistant_app: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setattr(assistant_routes, "get_current_user_context", _current_user)
    return assistant_app


# ── the wire ────────────────────────────────────────────────────────────────


def test_the_session_list_answers_both_of_its_shapes_whole(
    session_client: TestClient,
) -> None:
    """One route, two payloads: a bare array, and a page object under `page=1`.

    The console reads both (`listSessions` and `listSessionsPage` in
    `frontend/src/api.ts`), so a model that admits only one of them would make
    the other a 500.
    """
    listed = session_client.get("/api/v1/sessions")
    assert listed.status_code == 200
    assert listed.json() == _envelope([_SESSION])

    paged = session_client.get("/api/v1/sessions", params={"page": "1"})
    assert paged.status_code == 200
    assert paged.json() == _envelope(_SESSION_PAGE)


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("GET", "/api/v1/sessions/session-1", None, _SESSION),
        ("GET", "/api/v1/sessions/session-1/messages", None, _MESSAGE_PAGE),
        ("GET", "/api/v1/sessions/session-1/child-runs", None, _CHILD_RUN_PAGE),
        (
            "GET",
            "/api/v1/sessions/session-1/child-runs/child-1/messages",
            None,
            _CHILD_RUN_MESSAGES,
        ),
        (
            "POST",
            "/api/v1/sessions/session-1/permission-mode",
            {"permission_mode": "acceptEdits"},
            _PERMISSION_MODE,
        ),
        ("POST", "/api/v1/sessions/session-1/sandbox/terminate", None, _TERMINATION),
        ("POST", "/api/v1/sessions/session-1/recover", None, _SESSION),
        ("GET", "/api/v1/sessions/session-1/webshell", None, _WEBSHELL),
        ("DELETE", "/api/v1/sessions/session-1", None, _DELETION),
        ("POST", "/api/v1/sessions/session-1/archive", None, _ARCHIVAL),
        (
            "POST",
            "/api/v1/sessions/session-1/turn-inputs",
            {"content": "hello"},
            _TURN_RECEIPT,
        ),
        (
            "POST",
            "/api/v1/sessions/session-1/interaction-respond",
            {"interaction_id": "int-1", "answer": {"decline": False}},
            _INTERACTION_RECEIPT,
        ),
        ("POST", "/api/v1/sessions/session-1/interrupt", None, _INTERRUPT),
        (
            "POST",
            "/api/v1/sessions/session-1/child-runs/child-1/stop",
            None,
            _CHILD_RUN_STOP,
        ),
        ("POST", "/api/v1/sessions/session-1/conversation/end", None, _CONVERSATION_END),
    ],
)
def test_session_and_turn_routes_answer_the_service_payload_whole(
    session_client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    expected: dict[str, Any],
) -> None:
    response = session_client.request(method, path, json=body)
    assert response.status_code == 200
    assert response.json() == _envelope(expected)


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("POST", "/api/v1/assistants", {"display_name": "Research"}, _ASSISTANT),
        ("GET", "/api/v1/assistants", None, [_ASSISTANT]),
        ("GET", "/api/v1/assistants/asst_1", None, _ASSISTANT_DETAIL),
        ("PATCH", "/api/v1/assistants/asst_1", {"description": "x"}, _ASSISTANT),
        ("DELETE", "/api/v1/assistants/asst_1", None, _ASSISTANT_DELETION),
        (
            "POST",
            "/api/v1/assistants/asst_1/workspace/wake",
            None,
            _WORKSPACE_WAKE,
        ),
        (
            "POST",
            "/api/v1/assistants/asst_1/workspace/hibernate",
            None,
            _WORKSPACE_HIBERNATION,
        ),
        ("DELETE", "/api/v1/assistants/asst_1/workspace", None, _WORKSPACE_DESTRUCTION),
        (
            "POST",
            "/api/v1/assistants/asst_1/conversations",
            None,
            _STARTED_CONVERSATION,
        ),
    ],
)
def test_assistant_routes_answer_the_service_payload_whole(
    assistant_client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    expected: Any,
) -> None:
    response = assistant_client.request(method, path, json=body)
    assert response.status_code == 200
    assert response.json() == _envelope(expected)


# ── the documentation ───────────────────────────────────────────────────────


def _success_schema(app: FastAPI, path: str, method: str) -> dict[str, Any]:
    operation = app.openapi()["paths"][path][method]
    content = operation["responses"]["200"].get("content") or {}
    return content.get("application/json", {}).get("schema", {})


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/sessions", "get"),
        ("/api/v1/sessions/{session_id}", "get"),
        ("/api/v1/sessions/{session_id}", "delete"),
        ("/api/v1/sessions/{session_id}/messages", "get"),
        ("/api/v1/sessions/{session_id}/child-runs", "get"),
        (
            "/api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages",
            "get",
        ),
        ("/api/v1/sessions/{session_id}/permission-mode", "post"),
        ("/api/v1/sessions/{session_id}/sandbox/terminate", "post"),
        ("/api/v1/sessions/{session_id}/recover", "post"),
        ("/api/v1/sessions/{session_id}/webshell", "get"),
        ("/api/v1/sessions/{session_id}/archive", "post"),
        ("/api/v1/sessions/{session_id}/turn-inputs", "post"),
        ("/api/v1/sessions/{session_id}/interaction-respond", "post"),
        ("/api/v1/sessions/{session_id}/interrupt", "post"),
        (
            "/api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop",
            "post",
        ),
        ("/api/v1/sessions/{session_id}/conversation/end", "post"),
    ],
)
def test_every_json_session_route_documents_its_success_body(
    session_client: TestClient,
    path: str,
    method: str,
) -> None:
    assert _success_schema(session_client.app, path, method) != {}


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/assistants", "post"),
        ("/api/v1/assistants", "get"),
        ("/api/v1/assistants/{assistant_id}", "get"),
        ("/api/v1/assistants/{assistant_id}", "patch"),
        ("/api/v1/assistants/{assistant_id}", "delete"),
        ("/api/v1/assistants/{assistant_id}/workspace/wake", "post"),
        ("/api/v1/assistants/{assistant_id}/workspace/hibernate", "post"),
        ("/api/v1/assistants/{assistant_id}/workspace", "delete"),
        ("/api/v1/assistants/{assistant_id}/conversations", "post"),
    ],
)
def test_every_assistant_route_documents_its_success_body(
    assistant_client: TestClient,
    path: str,
    method: str,
) -> None:
    assert _success_schema(assistant_client.app, path, method) != {}


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/sessions/{session_id}/ai-stream", "post"),
        ("/api/v1/sessions/{session_id}/ai-stream", "get"),
        ("/api/v1/sessions/{session_id}/terminal/stream", "post"),
    ],
)
def test_the_streaming_routes_document_no_json_success_body(
    session_client: TestClient,
    path: str,
    method: str,
) -> None:
    """A ``StreamingResponse`` route must not publish a JSON 200 model.

    FastAPI sends a ``Response`` object as-is, so ``response_model`` there
    validates nothing and only advertises a body the route never sends. These
    three answer ``text/event-stream``, and ``GET .../ai-stream`` additionally
    answers a bare 204 when there is no turn to resume.
    """
    assert _success_schema(session_client.app, path, method) == {}


def test_the_session_envelope_is_named_once_for_each_payload(
    session_client: TestClient,
) -> None:
    """The envelope is declared generically, so each payload gets one component.

    A payload-shaped model would have deleted `code` and `message` from every
    response; naming `ApiEnvelope[...]` is what keeps the envelope on the wire
    and in the generated client.
    """
    schemas = session_client.app.openapi()["components"]["schemas"]
    assert "ApiEnvelope_SessionRecord_" in schemas
    assert "ApiEnvelope_SessionListPage_" in schemas
    assert {"code", "message", "data"} <= set(schemas["ApiEnvelope_SessionRecord_"]["properties"])
    # A persistence/runtime field must be added to the explicit public
    # projection before it can cross this response model.
    assert schemas["SessionRecord"]["additionalProperties"] is False
