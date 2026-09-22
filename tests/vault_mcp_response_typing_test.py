"""The typed Vault / MCP admin responses document the wire without changing it.

``response_model`` FILTERS the dict a handler returns: a field the model does
not declare is deleted from the response, and a field the model declares but
the handler omitted arrives as an explicit ``null``. Both are silent breaks for
a console that reads ``.data``, and neither shows up in a route test that only
checks the status code.

Each case here therefore compares the WHOLE body against the envelope the
service produced, so the assertion fails on a dropped key and on an added one.
The service views are stubbed with payloads that carry the two shapes a model
can get wrong: a credential whose optional key is absent, and a document with a
field no model declares.

The last group pins the documentation itself — a schema on the answering routes
and a body-less 204 on the deleting ones — because that is what this typing is
for, and a route can serve the right bytes while documenting nothing.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astrabox.api.routes import mcp_servers as mcp_server_routes
from astrabox.api.routes import mcp_tokens as mcp_token_routes
from astrabox.api.routes import vaults as vault_routes
from astrabox.core.service.orchestrator.credential_binding_service import (
    CredentialBindingService,
)
from astrabox.core.service.orchestrator.mcp_registry_service import MCPRegistryService
from astrabox.core.service.orchestrator.vault_service import VaultService

# ── payloads the stubbed services return ────────────────────────────────────
#
# `auth` holds only `type` and `mcp_server_url`: the eight remaining fields the
# credential model declares are absent here, so a response that injects any of
# them as `null` fails the comparison. `metadata` carries a nested object and
# `provisioned_by` is declared by no model, so a filtered field fails it too.

_CREDENTIAL: dict[str, Any] = {
    "credential_id": "vcr_1",
    "vault_id": "vlt_1",
    "display_name": "Issue tracker",
    "auth": {"type": "mcp_oauth", "mcp_server_url": "https://mcp.example.com/sse"},
    "archived_at": None,
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-01T00:00:00Z",
}

_CREDENTIAL_WITH_POLICY: dict[str, Any] = {
    "credential_id": "vcr_2",
    "vault_id": "vlt_1",
    "display_name": None,
    "auth": {
        "type": "mcp_static_header",
        "mcp_server_url": "https://tools.example.com/mcp",
        "header_name": "X-Api-Key",
        "networking": {"type": "limited", "allowed_hosts": ["tools.example.com"]},
        "injection_location": {"header": True, "body": False},
        "allowed_requests": {"methods": ["POST"], "paths": ["/mcp"]},
        "allow_insecure_http": False,
    },
    "archived_at": "2026-08-02T00:00:00Z",
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-02T00:00:00Z",
}

_VAULT: dict[str, Any] = {
    "vault_id": "vlt_1",
    "display_name": "Production tools",
    "metadata": {"purpose": "shared Agent credentials", "tier": {"level": 2}},
    "archived_at": None,
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-01T00:00:00Z",
}

_VAULT_WITH_CREDENTIALS: dict[str, Any] = {
    **_VAULT,
    "credentials": [_CREDENTIAL, _CREDENTIAL_WITH_POLICY],
}

_DELIVERY: dict[str, str] = {
    "deployment_mode": "trusted_private",
    "model_credentials": "egress_placeholder",
    "mcp_credentials": "egress_injection",
    "environment_credentials": "egress_placeholder",
}

_BINDING: dict[str, Any] = {
    "target_type": "agent",
    "target_id": "agent-1",
    "vault_ids": ["vlt_1"],
    "vaults": [_VAULT_WITH_CREDENTIALS],
}

_MCP_SERVER: dict[str, Any] = {
    "mcp_server_id": "mcp_1",
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-01T00:00:00Z",
    "org_id": "org-1",
    "name": "tracker",
    "description": None,
    "url": "https://mcp.example.com/mcp",
    "transport": "streamable_http",
    "enabled": True,
    "created_by": "admin-a",
    "updated_by": "admin-a",
    # Registered documents are returned whole, minus their private keys; a field
    # this surface has never declared must still reach the caller.
    "provisioned_by": "seed",
}

_ASSIGNMENT: dict[str, Any] = {
    "agent_id": "agent-1",
    "mcp_server_ids": ["mcp_1", "mcp_gone"],
    "mcp_servers": [_MCP_SERVER],
    "missing_mcp_server_ids": ["mcp_gone"],
}

_TOKEN: dict[str, Any] = {
    "token_id": "tok_1",
    "name": "ci-runner",
    "scope": "converse",
    "expires_at": None,
    "created_at": "2026-08-01T00:00:00Z",
    "last_used_at": None,
}


def _envelope(data: Any) -> dict[str, Any]:
    return {"code": "OK", "message": "success", "data": data}


def _returns(payload: Any) -> Any:
    async def _call(*args: Any, **kwargs: Any) -> Any:
        return payload

    return _call


async def _returns_none(*args: Any, **kwargs: Any) -> None:
    return None


# ── clients ─────────────────────────────────────────────────────────────────


@pytest.fixture
def vault_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(vault_routes, "_registered_on", None)
    monkeypatch.setattr(vault_routes, "credential_delivery_overview", lambda: dict(_DELIVERY))
    monkeypatch.setattr(VaultService, "create_vault", _returns(_VAULT))
    monkeypatch.setattr(VaultService, "get_vault", _returns(_VAULT))
    monkeypatch.setattr(VaultService, "archive_vault", _returns(_VAULT))
    monkeypatch.setattr(VaultService, "list_vaults", _returns([_VAULT_WITH_CREDENTIALS]))
    monkeypatch.setattr(VaultService, "delete_vault", _returns_none)
    monkeypatch.setattr(VaultService, "create_credential", _returns(_CREDENTIAL))
    monkeypatch.setattr(VaultService, "update_credential", _returns(_CREDENTIAL))
    monkeypatch.setattr(VaultService, "archive_credential", _returns(_CREDENTIAL))
    monkeypatch.setattr(
        VaultService, "list_credentials", _returns([_CREDENTIAL, _CREDENTIAL_WITH_POLICY])
    )
    monkeypatch.setattr(VaultService, "delete_credential", _returns_none)
    monkeypatch.setattr(CredentialBindingService, "ensure_vault_unbound", _returns_none)
    monkeypatch.setattr(CredentialBindingService, "get_agent_binding", _returns(_BINDING))
    monkeypatch.setattr(CredentialBindingService, "set_agent_binding", _returns(_BINDING))
    monkeypatch.setattr(CredentialBindingService, "get_assistant_binding", _returns(_BINDING))
    monkeypatch.setattr(CredentialBindingService, "set_assistant_binding", _returns(_BINDING))

    app = FastAPI()
    vault_routes.register_vault_routes(app)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def mcp_server_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(mcp_server_routes, "_registered_on", None)
    monkeypatch.setattr(MCPRegistryService, "create_server", _returns(_MCP_SERVER))
    monkeypatch.setattr(MCPRegistryService, "get_server", _returns(_MCP_SERVER))
    monkeypatch.setattr(MCPRegistryService, "update_server", _returns(_MCP_SERVER))
    monkeypatch.setattr(MCPRegistryService, "list_servers", _returns([_MCP_SERVER]))
    monkeypatch.setattr(MCPRegistryService, "delete_server", _returns_none)
    monkeypatch.setattr(MCPRegistryService, "get_agent_assignment", _returns(_ASSIGNMENT))
    monkeypatch.setattr(MCPRegistryService, "set_agent_assignment", _returns(_ASSIGNMENT))

    app = FastAPI()
    mcp_server_routes.register_mcp_server_routes(app)
    with TestClient(app) as client:
        yield client


class _TokenService:
    """The three calls the token routes make, with the service's own shapes."""

    async def issue(self, user: Any, **kwargs: Any) -> dict[str, Any]:
        return {**_TOKEN, "secret": "astrabox_mcp_abc"}

    async def list(self, user: Any) -> list[dict[str, Any]]:
        return [_TOKEN]

    async def revoke(self, user: Any, token_id: str) -> None:
        return None


@pytest.fixture
def mcp_token_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(mcp_token_routes, "_registered_on", None)
    app = FastAPI()
    mcp_token_routes.register_mcp_token_routes(app, service=_TokenService())
    with TestClient(app) as client:
        yield client


# ── the payload reaches the caller unchanged ────────────────────────────────


def test_a_vault_read_carries_every_field_the_service_produced(
    vault_client: TestClient,
) -> None:
    response = vault_client.get("/api/v1/admin/vaults/vlt_1")

    assert response.status_code == 200
    assert response.json() == _envelope(_VAULT)
    # A Vault the service did not describe with credentials must not gain the
    # key: `credentials: null` reads as "this Vault holds none".
    assert "credentials" not in response.json()["data"]


def test_the_vault_catalog_keeps_credentials_and_the_delivery_summary(
    vault_client: TestClient,
) -> None:
    response = vault_client.get("/api/v1/admin/vaults")

    assert response.status_code == 200
    assert response.json() == _envelope(
        {"vaults": [_VAULT_WITH_CREDENTIALS], "credential_delivery": _DELIVERY}
    )


def test_an_absent_credential_field_stays_absent(vault_client: TestClient) -> None:
    """The console distinguishes "no header name" from "header name is null"."""
    response = vault_client.get("/api/v1/admin/vaults/vlt_1/credentials")

    assert response.status_code == 200
    assert response.json() == _envelope(
        {"credentials": [_CREDENTIAL, _CREDENTIAL_WITH_POLICY]}
    )
    assert set(response.json()["data"]["credentials"][0]["auth"]) == {
        "type",
        "mcp_server_url",
    }


def test_creating_and_updating_a_credential_answer_the_same_shape(
    vault_client: TestClient,
) -> None:
    created = vault_client.post(
        "/api/v1/admin/vaults/vlt_1/credentials",
        json={"auth": {"type": "mcp_oauth"}, "display_name": "Issue tracker"},
    )
    updated = vault_client.patch(
        "/api/v1/admin/vaults/vlt_1/credentials/vcr_1", json={"display_name": "Renamed"}
    )
    archived = vault_client.post(
        "/api/v1/admin/vaults/vlt_1/credentials/vcr_1/archive"
    )

    assert created.status_code == 200
    assert created.json() == _envelope(_CREDENTIAL)
    assert updated.json() == created.json()
    assert archived.json() == created.json()


def test_creating_and_archiving_a_vault_answer_the_vault(vault_client: TestClient) -> None:
    created = vault_client.post("/api/v1/admin/vaults", json={"display_name": "Production tools"})
    archived = vault_client.post("/api/v1/admin/vaults/vlt_1/archive")

    assert created.status_code == 200
    assert created.json() == _envelope(_VAULT)
    assert archived.json() == _envelope(_VAULT)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/agents/agent-1/credential-vaults"),
        ("PUT", "/api/v1/admin/agents/agent-1/credential-vaults"),
        ("GET", "/api/v1/admin/assistants/asst-1/credential-vaults"),
        ("PUT", "/api/v1/admin/assistants/asst-1/credential-vaults"),
    ],
)
def test_a_credential_binding_carries_its_vaults_whole(
    vault_client: TestClient, method: str, path: str
) -> None:
    response = vault_client.request(
        method, path, json={"vault_ids": ["vlt_1"]} if method == "PUT" else None
    )

    assert response.status_code == 200
    assert response.json() == _envelope(_BINDING)


def test_a_registered_mcp_server_reaches_the_caller_with_its_unknown_fields(
    mcp_server_client: TestClient,
) -> None:
    """The registry answers the stored document; a model must not narrow it."""
    created = mcp_server_client.post(
        "/api/v1/admin/mcp-servers", json={"name": "tracker", "url": "https://x/mcp"}
    )
    fetched = mcp_server_client.get("/api/v1/admin/mcp-servers/mcp_1")
    listed = mcp_server_client.get("/api/v1/admin/mcp-servers")
    updated = mcp_server_client.patch(
        "/api/v1/admin/mcp-servers/mcp_1", json={"enabled": False}
    )

    assert created.status_code == 200
    assert created.json() == _envelope(_MCP_SERVER)
    assert fetched.json() == _envelope(_MCP_SERVER)
    assert updated.json() == _envelope(_MCP_SERVER)
    assert listed.json() == _envelope({"mcp_servers": [_MCP_SERVER]})
    assert created.json()["data"]["provisioned_by"] == "seed"


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_an_agent_assignment_reports_the_servers_that_are_gone(
    mcp_server_client: TestClient, method: str
) -> None:
    response = mcp_server_client.request(
        method,
        "/api/v1/admin/agents/agent-1/mcp-servers",
        json={"mcp_server_ids": ["mcp_1"]} if method == "PUT" else None,
    )

    assert response.status_code == 200
    assert response.json() == _envelope(_ASSIGNMENT)


def test_minting_a_token_answers_201_with_the_secret(mcp_token_client: TestClient) -> None:
    """The secret is in the mint's answer and in no later one."""
    response = mcp_token_client.post("/api/v1/mcp-tokens", json={"name": "ci-runner"})

    assert response.status_code == 201
    assert response.json() == _envelope({**_TOKEN, "secret": "astrabox_mcp_abc"})


def test_listing_tokens_carries_no_secret_field(mcp_token_client: TestClient) -> None:
    response = mcp_token_client.get("/api/v1/mcp-tokens")

    assert response.status_code == 200
    assert response.json() == _envelope({"tokens": [_TOKEN]})
    assert "secret" not in response.json()["data"]["tokens"][0]


# ── the deleting routes answer 204 with no body ─────────────────────────────


@pytest.mark.parametrize(
    ("fixture_name", "path"),
    [
        ("vault_client", "/api/v1/admin/vaults/vlt_1"),
        ("vault_client", "/api/v1/admin/vaults/vlt_1/credentials/vcr_1"),
        ("mcp_server_client", "/api/v1/admin/mcp-servers/mcp_1"),
        ("mcp_token_client", "/api/v1/mcp-tokens/tok_1"),
    ],
)
def test_a_delete_answers_204_and_documents_204(
    request: pytest.FixtureRequest, fixture_name: str, path: str
) -> None:
    """The status the caller receives and the status the schema promises agree."""
    client: TestClient = request.getfixturevalue(fixture_name)

    response = client.delete(path)

    assert response.status_code == 204
    assert response.content == b""

    schema_path = path.replace("vlt_1", "{vault_id}")
    schema_path = schema_path.replace("vcr_1", "{credential_id}")
    schema_path = schema_path.replace("mcp_1", "{mcp_server_id}")
    schema_path = schema_path.replace("tok_1", "{token_id}")
    responses = client.app.openapi()["paths"][schema_path]["delete"]["responses"]
    assert "200" not in responses
    assert responses["204"] == {"description": "Successful Response"}


# ── the answering routes document a schema ──────────────────────────────────


@pytest.mark.parametrize(
    ("fixture_name", "expected_answers"),
    [("vault_client", 13), ("mcp_server_client", 6), ("mcp_token_client", 2)],
)
def test_every_answering_route_documents_the_envelope_it_returns(
    request: pytest.FixtureRequest, fixture_name: str, expected_answers: int
) -> None:
    """An untyped success answer is what this surface is being brought out of.

    ``{}`` — the schema FastAPI emits for a handler with no ``response_model`` —
    tells a generated client nothing at all, so every route that answers a body
    must name the model it answers with, whichever 2xx it answers under.
    """
    client: TestClient = request.getfixturevalue(fixture_name)
    schema = client.app.openapi()

    answers: list[str] = []
    untyped: list[str] = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for status, answer in operation["responses"].items():
                if not status.startswith("2") or "content" not in answer:
                    continue  # a 204 route carries no body; covered above
                answers.append(f"{method.upper()} {path} -> {status}")
                if "$ref" not in answer["content"]["application/json"]["schema"]:
                    untyped.append(f"{method.upper()} {path} -> {status}")

    assert untyped == []
    # Guard the guard: an empty list of answers satisfies the check above
    # without a single route having been examined.
    assert len(answers) == expected_answers


def test_the_envelope_models_declare_code_message_and_data(
    vault_client: TestClient,
) -> None:
    """A payload-shaped model would delete ``code`` and ``message`` from the wire."""
    schema = vault_client.app.openapi()
    ref = schema["paths"]["/api/v1/admin/vaults/{vault_id}"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"]
    model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]

    assert set(model["required"]) == {"code", "message", "data"}
