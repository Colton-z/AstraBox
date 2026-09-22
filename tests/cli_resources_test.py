"""Apply, diff and destroy semantics against a recording deployment.

The fake deployment records every request, so each test can assert not only the
reported verdict but that the right number of writes — often zero — reached the
API. A verdict that is right while the writes are wrong is the failure these
tests exist to catch.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from astrabox.cli.client import ApiClient, Endpoint
from astrabox.cli.document import DOCUMENT_VERSION, parse_document
from astrabox.cli.output import EXIT_CONFLICT, CliError
from astrabox.cli.resources import (
    ACTION_ABSENT,
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_RETAINED,
    ACTION_UNCHANGED,
    ACTION_UPDATE,
    _cmd_destroy,
    plan_and_run,
)

_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

#: The authoring contract a deployment serves. `display_name` is nested in the
#: stored document exactly as the real Agent schema nests it.
_AGENT_FIELDS = [
    {"key": "name", "type": "string", "required": True},
    {"key": "model", "type": "string", "required": True},
    {"key": "environment_name", "type": "env_ref", "required": True},
    {"key": "display_name", "type": "string", "path": "display_meta.display_name"},
    {"key": "enabled", "type": "boolean"},
]
_ENVIRONMENT_FIELDS = [
    {"key": "name", "type": "string", "required": True},
    {"key": "engine_kind", "type": "enum", "required": True},
    {"key": "endpoint_provider", "type": "enum"},
    {"key": "enabled", "type": "boolean"},
]


class _Deployment:
    """A deployment that answers the routes apply reads and writes."""

    def __init__(
        self,
        *,
        agents: list[dict[str, Any]] | None = None,
        environments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.agents = agents or []
        self.environments = environments or []
        self.requests: list[tuple[str, str, Any]] = []

    def client(self) -> ApiClient:
        return ApiClient(
            Endpoint(base_url="http://deployment.test", token=None),
            transport=httpx.MockTransport(self._handle),
        )

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [entry for entry in self.requests if entry[0] in _WRITE_METHODS]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path

        if request.method == "GET" and path == "/api/v1/admin/agent-schema":
            return _envelope({"version": 1, "groups": [], "fields": _AGENT_FIELDS})
        if request.method == "GET" and path == "/api/v1/admin/environment-schema":
            return _envelope({"version": 1, "groups": [], "fields": _ENVIRONMENT_FIELDS})
        if request.method == "GET" and path == "/api/v1/agents":
            return _envelope(self.agents)
        if request.method == "GET" and path == "/api/v1/admin/environments":
            return _envelope(self.environments)
        if request.method == "POST" and path == "/api/v1/agents":
            created = {"agent_id": f"id-{len(self.agents)}", "version": 1, **(body or {})}
            self.agents.append(created)
            return _envelope(created)
        if request.method == "PUT" and path.startswith("/api/v1/agents/"):
            return _envelope({"agent_id": path.rsplit("/", 1)[-1], **(body or {})})
        if request.method == "PUT" and path.startswith("/api/v1/admin/environments/"):
            return _envelope({"name": path.rsplit("/", 1)[-1], **(body or {})})
        if request.method == "DELETE" and path.startswith("/api/v1/agents/"):
            return _envelope({"deleted": True})
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": path, "data": None})


def _envelope(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": "OK", "message": "success", "data": data})


def _document(**overrides: Any):
    raw: dict[str, Any] = {
        "version": DOCUMENT_VERSION,
        "environments": [{"name": "default", "endpoint_provider": "litellm"}],
        "agents": [
            {"name": "researcher", "model": "claude-opus-5", "environment_name": "default"}
        ],
    }
    raw.update(overrides)
    return parse_document(raw)


def test_apply_creates_a_missing_agent_and_environment() -> None:
    deployment = _Deployment()

    with deployment.client() as client:
        results = plan_and_run(client, _document(), write=True)

    assert [row["action"] for row in results] == [ACTION_CREATE, ACTION_CREATE]
    assert [(method, path) for method, path, _ in deployment.writes] == [
        ("PUT", "/api/v1/admin/environments/default"),
        ("POST", "/api/v1/agents"),
    ]


def test_apply_writes_the_environment_before_the_agent() -> None:
    """The Agent's environment_name is validated against an existing
    environment, so a first-run document that applied in declaration order
    would be rejected by the deployment."""
    deployment = _Deployment()

    with deployment.client() as client:
        plan_and_run(client, _document(), write=True)

    paths = [path for method, path, _ in deployment.writes]
    assert paths.index("/api/v1/admin/environments/default") < paths.index("/api/v1/agents")


def test_apply_updates_an_existing_agent_and_fences_on_the_stored_version() -> None:
    """Sending the stored version turns a document written against a stale read
    into a 409 instead of an overwrite of somebody else's edit."""
    deployment = _Deployment(
        agents=[
            {
                "agent_id": "agent-1",
                "version": 7,
                "name": "researcher",
                "model": "claude-sonnet-5",
                "environment_name": "default",
            }
        ],
        environments=[{"name": "default", "endpoint_provider": "litellm"}],
    )

    with deployment.client() as client:
        results = plan_and_run(client, _document(), write=True)

    agent_result = next(row for row in results if row["kind"] == "agent")
    assert agent_result["action"] == ACTION_UPDATE
    assert agent_result["fields"] == ["model"]
    method, path, body = deployment.writes[0]
    assert (method, path) == ("PUT", "/api/v1/agents/agent-1")
    assert body["version"] == 7
    assert body["model"] == "claude-opus-5"


def test_apply_reports_unchanged_and_sends_no_write() -> None:
    """Re-applying an unchanged document must converge without touching the
    deployment; a write here would bump versions on every run."""
    deployment = _Deployment(
        agents=[
            {
                "agent_id": "agent-1",
                "version": 3,
                "name": "researcher",
                "model": "claude-opus-5",
                "environment_name": "default",
            }
        ],
        environments=[{"name": "default", "endpoint_provider": "litellm"}],
    )

    with deployment.client() as client:
        results = plan_and_run(client, _document(), write=True)

    assert [row["action"] for row in results] == [ACTION_UNCHANGED, ACTION_UNCHANGED]
    assert deployment.writes == []


def test_diff_sends_no_writes_while_reporting_the_same_verdicts() -> None:
    deployment = _Deployment()

    with deployment.client() as client:
        results = plan_and_run(client, _document(), write=False)

    assert [row["action"] for row in results] == [ACTION_CREATE, ACTION_CREATE]
    assert deployment.writes == []
    assert deployment.requests, "diff must still read the deployment"


def test_apply_refuses_an_ambiguous_agent_name_without_writing() -> None:
    """The deployment does not constrain Agent names to be unique. Picking one
    of two matches would make the same document mean different things on two
    deployments, so apply stops — and stops before writing anything."""
    deployment = _Deployment(
        agents=[
            {"agent_id": "agent-1", "version": 1, "name": "researcher"},
            {"agent_id": "agent-2", "version": 1, "name": "researcher"},
        ],
        environments=[{"name": "default", "endpoint_provider": "litellm"}],
    )

    with deployment.client() as client:
        with pytest.raises(CliError) as caught:
            plan_and_run(client, _document(), write=True)

    assert caught.value.exit_code == EXIT_CONFLICT
    assert caught.value.details["agent_ids"] == ["agent-1", "agent-2"]
    assert deployment.writes == []


def test_the_environment_body_omits_the_name_carried_in_the_path() -> None:
    """`PUT /admin/environments/{name}` takes the name from the path and drops
    it from the payload, so sending it back would be a field the deployment
    discards."""
    deployment = _Deployment()

    with deployment.client() as client:
        plan_and_run(client, _document(), write=True)

    _, _, body = deployment.writes[0]
    assert "name" not in body
    assert body["endpoint_provider"] == "litellm"


def test_destroy_deletes_agents_and_reports_environments_as_retained(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The deployment serves no delete route for an environment preset.
    Reporting the removal anyway would tell a caller its deployment is clean
    when the environment is still there."""
    deployment = _Deployment(
        agents=[{"agent_id": "agent-1", "version": 1, "name": "researcher"}],
        environments=[{"name": "default"}],
    )
    args = _destroy_args()
    _wire(monkeypatch, deployment)

    assert _cmd_destroy(args) == 0

    rows = json.loads(capsys.readouterr().out)["resources"]
    assert {row["name"]: row["action"] for row in rows} == {
        "researcher": ACTION_DELETE,
        "default": ACTION_RETAINED,
    }
    assert [(method, path) for method, path, _ in deployment.writes] == [
        ("DELETE", "/api/v1/agents/agent-1")
    ]


def test_destroy_reports_an_absent_agent_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    deployment = _Deployment(environments=[{"name": "default"}])
    args = _destroy_args()
    _wire(monkeypatch, deployment)

    assert _cmd_destroy(args) == 0

    rows = json.loads(capsys.readouterr().out)["resources"]
    assert next(row for row in rows if row["name"] == "researcher")["action"] == ACTION_ABSENT
    assert deployment.writes == []


def test_a_nested_field_is_compared_where_the_deployment_stores_it() -> None:
    """The schema says display_name lives at display_meta.display_name. Looking
    for it at the top level finds nothing, so an unchanged document would
    report that field as changed on every single run — and an export of a
    deployment would never converge against the deployment it came from."""
    deployment = _Deployment(
        agents=[
            {
                "agent_id": "agent-1",
                "version": 2,
                "name": "researcher",
                "model": "claude-opus-5",
                "environment_name": "default",
                "display_meta": {"display_name": "Researcher"},
            }
        ],
        environments=[{"name": "default", "endpoint_provider": "litellm"}],
    )
    document = _document(
        agents=[
            {
                "name": "researcher",
                "model": "claude-opus-5",
                "environment_name": "default",
                "display_name": "Researcher",
            }
        ]
    )

    with deployment.client() as client:
        results = plan_and_run(client, document, write=True)

    agent_result = next(row for row in results if row["kind"] == "agent")
    assert agent_result["action"] == ACTION_UNCHANGED, (
        f"nested field reported as changed: {agent_result['fields']}"
    )
    assert deployment.writes == []


def test_a_nested_field_that_really_differs_is_still_reported() -> None:
    """The path lookup must not turn into "never changed"."""
    deployment = _Deployment(
        agents=[
            {
                "agent_id": "agent-1",
                "version": 2,
                "name": "researcher",
                "model": "claude-opus-5",
                "environment_name": "default",
                "display_meta": {"display_name": "Old name"},
            }
        ],
        environments=[{"name": "default", "endpoint_provider": "litellm"}],
    )
    document = _document(
        agents=[
            {
                "name": "researcher",
                "model": "claude-opus-5",
                "environment_name": "default",
                "display_name": "New name",
            }
        ]
    )

    with deployment.client() as client:
        results = plan_and_run(client, document, write=True)

    agent_result = next(row for row in results if row["kind"] == "agent")
    assert agent_result["action"] == ACTION_UPDATE
    assert agent_result["fields"] == ["display_name"]


def _wire(monkeypatch: pytest.MonkeyPatch, deployment: _Deployment) -> None:
    """Point the subcommand's client factory at the recording deployment."""
    monkeypatch.setattr(
        "astrabox.cli.resources._client", lambda _args: deployment.client()
    )


def _destroy_args() -> Any:
    """The parsed-args object `_cmd_destroy` reads, over a real document file."""
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    document_path = Path(tempfile.mkdtemp()) / "astrabox.yaml"
    document_path.write_text(
        "version: 1\n"
        "environments:\n"
        "  - name: default\n"
        "agents:\n"
        "  - name: researcher\n"
        "    model: claude-opus-5\n"
        "    environment_name: default\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        file=str(document_path),
        yes=True,
        output="json",
        endpoint="http://deployment.test",
        token=None,
    )
