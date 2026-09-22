"""``astrabox init`` — the skeleton, and the export driven by the server schema.

The export's contract is a round trip: whatever it writes, `astrabox apply`
must accept. That is what these tests assert, rather than the exact field list
— which belongs to the deployment and moves without this file changing.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from astrabox.cli.client import ApiClient, Endpoint
from astrabox.cli.document import parse_document
from astrabox.cli.output import EXIT_CONFLICT, EXIT_USAGE, CliError
from astrabox.cli.scaffold import _cmd_init, export_document

_AGENT_SCHEMA = {
    "version": 3,
    "groups": [],
    "fields": [
        {"key": "name", "type": "string", "required": True},
        {"key": "display_name", "type": "string", "path": "display_meta.display_name"},
        {"key": "model", "type": "string", "required": True},
        {"key": "environment_name", "type": "env_ref", "required": True},
        {"key": "enabled", "type": "boolean"},
    ],
}
_ENVIRONMENT_SCHEMA = {
    "version": 2,
    "groups": [],
    "fields": [
        {"key": "name", "type": "string", "required": True},
        {"key": "engine_kind", "type": "enum", "required": True},
        {"key": "provider_access", "type": "object"},
    ],
}
_STORED_AGENT = {
    # Server-managed state the write path refuses as an unknown field.
    "agent_id": "agent-1",
    "version": 4,
    "created_by": "someone",
    "updated_at": "2026-08-25T00:00:00Z",
    "state": "ACTIVE",
    # Declared fields, one of them nested under its schema path.
    "name": "researcher",
    "display_meta": {"display_name": "Researcher", "icon": "book"},
    "model": "claude-opus-5",
    "environment_name": "default",
    "enabled": True,
}
_STORED_ENVIRONMENT = {
    "name": "default",
    "engine_kind": "claude_code",
    "provider_access": {"api_key": "sk-****"},
    "updated_by": "someone",
}


@pytest.fixture(autouse=True)
def _isolated_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer shell holding a partial client credential must not decide
    what these tests exercise."""
    for name in (
        "ASTRABOX_ENDPOINT",
        "ASTRABOX_SERVER_HOST_PORT",
        "ASTRABOX_TOKEN",
        "ASTRABOX_CLIENT_ID",
        "ASTRABOX_CLIENT_SECRET",
        "ASTRABOX_TOKEN_URL",
        "ASTRABOX_SCOPE",
    ):
        monkeypatch.delenv(name, raising=False)


def _client(*, agents: list[dict[str, Any]] | None = None) -> ApiClient:
    def handle(request: httpx.Request) -> httpx.Response:
        routes = {
            "/api/v1/admin/agent-schema": _AGENT_SCHEMA,
            "/api/v1/admin/environment-schema": _ENVIRONMENT_SCHEMA,
            "/api/v1/agents": [_STORED_AGENT] if agents is None else agents,
            "/api/v1/admin/environments": [_STORED_ENVIRONMENT],
        }
        data = routes.get(request.url.path)
        if data is None:
            return httpx.Response(404, json={"code": "NOT_FOUND", "message": "x", "data": None})
        return httpx.Response(200, json={"code": "OK", "message": "ok", "data": data})

    return ApiClient(
        Endpoint(base_url="http://deployment.test", token=None),
        transport=httpx.MockTransport(handle),
    )


def test_the_export_is_a_document_apply_accepts() -> None:
    """The round trip is the whole point: an export that apply then rejects
    would be worse than no export at all."""
    with _client() as client:
        document = export_document(client)

    parsed = parse_document(document)

    assert [spec.name for spec in parsed.environments] == ["default"]
    assert [spec.name for spec in parsed.agents] == ["researcher"]


def test_server_managed_state_never_reaches_the_document() -> None:
    """agent_id, version, created_by and friends are not writable fields; the
    write path refuses them, so exporting them would break the next apply."""
    with _client() as client:
        document = export_document(client)

    exported = document["agents"][0]
    for managed in ("agent_id", "version", "created_by", "updated_at", "state"):
        assert managed not in exported


def test_a_nested_field_is_exported_under_the_name_it_is_written_with() -> None:
    """The schema says display_name lives at display_meta.display_name in the
    stored document; the document must carry the writable key, not the path."""
    with _client() as client:
        document = export_document(client)

    exported = document["agents"][0]
    assert exported["display_name"] == "Researcher"
    assert "display_meta" not in exported


def test_a_field_absent_from_the_schema_is_dropped_even_when_stored() -> None:
    """`icon` sits beside display_name in storage but is not in this
    deployment's schema, so it is not the CLI's to write back."""
    with _client() as client:
        document = export_document(client)

    assert "icon" not in document["agents"][0]


def test_a_masked_secret_is_exported_as_the_mask() -> None:
    """Sending the mask back unmodified is how the write path is told to keep
    the stored secret; rewriting it here would send a literal '****'."""
    with _client() as client:
        document = export_document(client)

    assert document["environments"][0]["provider_access"] == {"api_key": "sk-****"}


def test_init_writes_a_skeleton_without_contacting_anything(tmp_path: Path, capsys) -> None:
    target = tmp_path / "astrabox.yaml"

    assert _cmd_init(_args(target, from_deployment=False)) == 0

    parsed = parse_document(_load_yaml(target))
    assert [spec.name for spec in parsed.environments] == ["default"]
    assert json.loads(capsys.readouterr().out)["file"] == str(target)


def test_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """An init that silently replaced a document would discard configuration
    that exists nowhere else."""
    target = tmp_path / "astrabox.yaml"
    target.write_text("version: 1\n", encoding="utf-8")

    with pytest.raises(CliError) as caught:
        _cmd_init(_args(target, from_deployment=False))

    assert caught.value.exit_code == EXIT_USAGE
    assert target.read_text(encoding="utf-8") == "version: 1\n"


def test_init_overwrites_with_force(tmp_path: Path, capsys) -> None:
    target = tmp_path / "astrabox.yaml"
    target.write_text("version: 1\n", encoding="utf-8")

    assert _cmd_init(_args(target, from_deployment=False, force=True)) == 0

    assert "agents:" in target.read_text(encoding="utf-8")
    capsys.readouterr()


def test_init_from_deployment_writes_the_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    target = tmp_path / "astrabox.yaml"
    monkeypatch.setattr("astrabox.cli.scaffold.ApiClient", lambda *_a, **_k: _client())

    assert _cmd_init(_args(target, from_deployment=True)) == 0

    parsed = parse_document(_load_yaml(target))
    assert [spec.name for spec in parsed.agents] == ["researcher"]
    assert json.loads(capsys.readouterr().out)["agents"] == 1


def test_init_refuses_ambiguous_agent_names_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "astrabox.yaml"
    duplicate = {**_STORED_AGENT, "agent_id": "agent-2"}
    monkeypatch.setattr(
        "astrabox.cli.scaffold.ApiClient",
        lambda *_a, **_k: _client(agents=[_STORED_AGENT, duplicate]),
    )

    with pytest.raises(CliError) as caught:
        _cmd_init(_args(target, from_deployment=True))

    assert caught.value.exit_code == EXIT_CONFLICT
    assert caught.value.details == {
        "conflicts": [
            {"name": "researcher", "agent_ids": ["agent-1", "agent-2"]}
        ]
    }
    assert not target.exists()


def _args(target: Path, *, from_deployment: bool, force: bool = False) -> Any:
    return SimpleNamespace(
        file=str(target),
        from_deployment=from_deployment,
        force=force,
        output="json",
        endpoint="http://deployment.test",
        token=None,
    )


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))
