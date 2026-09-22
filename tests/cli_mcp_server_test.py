"""``astrabox mcp serve`` — the MCP façade over the same command layer.

What matters here is that the façade adds no second behaviour: a tool call
lands on the function the argv command calls. The protocol details that are
asserted are the ones a client breaks on — a notification must draw no
response, and a tool that ran and failed must come back as a tool result, not
as a transport error.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from astrabox.cli import mcp_server
from astrabox.cli.client import Endpoint

_ENDPOINT = Endpoint(base_url="http://deployment.test", token=None)


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    return mcp_server._handle(_ENDPOINT, json.dumps(message))


def test_initialize_echoes_a_protocol_version_the_vendor_supports() -> None:
    """The candidate set is the MCP package's, not one this repository keeps a
    copy of the MCP SDK's version registry."""
    from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS

    supported = sorted(SUPPORTED_PROTOCOL_VERSIONS)[0]
    response = _handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": supported}}
    )

    assert response is not None
    assert response["result"]["protocolVersion"] == supported
    assert response["result"]["serverInfo"]["name"] == "astrabox-platform"


def test_an_unknown_protocol_version_falls_back_to_the_latest() -> None:
    from mcp_types.version import LATEST_PROTOCOL_VERSION

    response = _handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01"}}
    )

    assert response is not None
    assert response["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_a_notification_draws_no_response() -> None:
    """A response to a notification is a protocol violation the client sees as
    an unsolicited message."""
    assert _handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unknown_method_is_a_jsonrpc_error() -> None:
    response = _handle({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})

    assert response is not None
    assert response["error"]["code"] == -32601


def test_every_tool_declares_an_object_input_schema() -> None:
    """A client builds its call form from these; a tool without a schema is one
    the model cannot call correctly."""
    for tool in mcp_server.tool_definitions():
        assert tool["name"].startswith("astrabox_")
        assert tool["description"].strip()
        assert tool["inputSchema"]["type"] == "object"


def test_tools_list_carries_the_same_definitions() -> None:
    response = _handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})

    assert response is not None
    assert [tool["name"] for tool in response["result"]["tools"]] == [
        tool["name"] for tool in mcp_server.tool_definitions()
    ]


def test_a_tool_that_ran_and_failed_comes_back_as_a_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP separates a protocol fault from a tool that failed: the second is a
    result with isError, so the model sees the reason instead of the client
    seeing a broken transport."""
    response = _handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "astrabox_schema", "arguments": {"kind": "nonsense"}}}
    )

    assert response is not None
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "kind must be one of" in response["result"]["structuredContent"]["error"]


def test_malformed_arguments_are_a_protocol_error() -> None:
    response = _handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "astrabox_get", "arguments": ["not", "an", "object"]}}
    )

    assert response is not None
    assert response["error"]["code"] == -32602


def test_apply_lands_on_the_same_planner_the_command_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One implementation, two façades: if this ever stopped calling
    plan_and_run, the tool and the command could drift into two behaviours."""
    seen: dict[str, Any] = {}

    def fake_plan(_client: Any, document: Any, *, write: bool) -> list[dict[str, Any]]:
        seen["names"] = [spec.name for spec in document.resources()]
        seen["write"] = write
        return [{"kind": "agent", "name": "researcher", "action": "create", "fields": []}]

    monkeypatch.setattr(mcp_server, "plan_and_run", fake_plan)
    monkeypatch.setattr(mcp_server, "ApiClient", lambda *_a, **_k: _NullClient())

    payload = mcp_server.call_tool(
        _ENDPOINT,
        "astrabox_apply",
        {"document": {"version": 1, "agents": [{"name": "researcher", "model": "m"}]}},
    )

    assert seen == {"names": ["researcher"], "write": True}
    assert payload["dry_run"] is False


def test_diff_asks_the_planner_not_to_write(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_plan(_client: Any, _document: Any, *, write: bool) -> list[dict[str, Any]]:
        seen["write"] = write
        return []

    monkeypatch.setattr(mcp_server, "plan_and_run", fake_plan)
    monkeypatch.setattr(mcp_server, "ApiClient", lambda *_a, **_k: _NullClient())

    payload = mcp_server.call_tool(
        _ENDPOINT, "astrabox_diff", {"document": {"version": 1, "agents": []}}
    )

    assert seen["write"] is False
    assert payload["dry_run"] is True


def test_an_invalid_document_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "ApiClient", lambda *_a, **_k: _NullClient())

    response = _handle(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "astrabox_apply", "arguments": {"document": {"version": 99}}}}
    )

    assert response is not None
    assert response["result"]["isError"] is True


def test_status_reports_both_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "ApiClient", lambda *_a, **_k: _NullClient())
    monkeypatch.setattr(
        mcp_server,
        "probe",
        lambda _base, path: (path == "/healthz", "HTTP 200" if path == "/healthz" else "HTTP 503"),
    )

    payload = mcp_server.call_tool(_ENDPOINT, "astrabox_status", {})

    assert payload["healthy"] is True
    assert payload["ready"] is False


def test_get_returns_the_collection_items(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": "OK", "message": "ok", "data": [{"name": "researcher"}]},
        )

    from astrabox.cli.client import ApiClient

    monkeypatch.setattr(
        mcp_server,
        "ApiClient",
        lambda *_a, **_k: ApiClient(_ENDPOINT, transport=httpx.MockTransport(handle)),
    )

    payload = mcp_server.call_tool(_ENDPOINT, "astrabox_get", {"kind": "agents"})

    assert payload["items"] == [{"name": "researcher"}]


class _NullClient:
    """A client that must not be called: these tests stub the layer beneath."""

    def __enter__(self) -> _NullClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected client call: {name}")
