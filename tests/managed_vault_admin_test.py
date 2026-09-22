"""Credential Vault is an admin resource, independent from user identity."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from astrabox.api.routes import vaults as vault_routes
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.credential_binding_service import (
    CredentialBindingService,
)
from astrabox.core.service.orchestrator.vault_service import VaultService


class _VaultRepo:
    def __init__(self) -> None:
        self.vaults: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, list[dict[str, Any]]] = {}
        self.created: dict[str, Any] | None = None

    async def create_vault(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.created = dict(doc)
        stored = {
            "vault_id": "vlt-managed",
            "archived_at": None,
            "created_at": "now",
            "updated_at": "now",
            **doc,
        }
        self.vaults["vlt-managed"] = stored
        return stored

    async def get_vault(self, vault_id: str) -> dict[str, Any] | None:
        return self.vaults.get(vault_id)

    async def list_vaults(self, *, org_id: str, limit: int = 200) -> list[dict[str, Any]]:
        _ = limit
        return [v for v in self.vaults.values() if v.get("org_id") == org_id]

    async def list_credentials(self, vault_id: str) -> list[dict[str, Any]]:
        return list(self.credentials.get(vault_id, []))


def test_vault_http_surface_exists_only_under_admin_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault_routes, "_registered_on", None)
    app = FastAPI()
    vault_routes.register_vault_routes(app)

    paths = {
        route.path
        for route in app.routes
        if "vault" in getattr(route, "path", "")
    }

    assert paths
    assert all(path.startswith("/api/v1/admin/") for path in paths)
    assert "/api/v1/vaults" not in paths


async def test_vault_is_org_managed_not_owned_by_the_admin_user() -> None:
    repo = _VaultRepo()
    service = VaultService(vault_repo=repo)  # type: ignore[arg-type]
    creator = UserContext(user_id="admin-a", org_id="org-1", roles=["admin"])

    created = await service.create_vault(
        creator,
        display_name="Production tools",
        metadata={"purpose": "shared Agent credentials"},
    )

    assert created["vault_id"] == "vlt-managed"
    assert repo.created == {
        "org_id": "org-1",
        "created_by": "admin-a",
        "display_name": "Production tools",
        "metadata": {"purpose": "shared Agent credentials"},
    }
    assert "owner_user_id" not in repo.created

    # Another administrator in the same organization manages the same Vault;
    # the login user is audit context, never the Vault's owner/selection key.
    peer = UserContext(user_id="admin-b", org_id="org-1", roles=["admin"])
    assert (await service.get_vault(peer, "vlt-managed"))["vault_id"] == "vlt-managed"

    outsider = UserContext(user_id="admin-c", org_id="org-2", roles=["admin"])
    with pytest.raises(APIError) as hidden:
        await service.get_vault(outsider, "vlt-managed")
    assert hidden.value.status_code == 404


class _BindingVaultService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.authorized: list[tuple[str, str]] = []

    async def get_vault(
        self, user: UserContext, vault_id: str
    ) -> dict[str, Any]:
        self.authorized.append((user.user_id, vault_id))
        return {"vault_id": vault_id}

    async def validate_binding_ids(
        self,
        user: UserContext,
        vault_ids: list[str],
        *,
        target_type: str,
    ) -> list[str]:
        self.calls.append(
            {
                "user": user,
                "vault_ids": list(vault_ids),
                "target_type": target_type,
            }
        )
        return ["vlt-b", "vlt-a"]

    async def describe_binding(
        self, user: UserContext, vault_ids: list[str]
    ) -> list[dict[str, Any]]:
        _ = user
        return [{"vault_id": vault_id} for vault_id in vault_ids]


class _AgentRepo:
    def __init__(self, extra: list[dict[str, Any]] | None = None) -> None:
        # The internal name and the display name differ on purpose. An Agent
        # keeps its display name under display_meta; a holder list that reads
        # the top level instead falls back to "reviewer-internal", so these
        # assertions distinguish the two paths.
        self.doc = {
            "agent_id": "agent-1",
            "name": "reviewer-internal",
            "display_meta": {"display_name": "Reviewer"},
        }
        #: Further Agents bound to the same Vault, for the many-holders case.
        self.extra: list[dict[str, Any]] = extra or []
        self.updates: dict[str, Any] | None = None

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.doc) if agent_id == "agent-1" else None

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        assert agent_id == "agent-1"
        self.updates = dict(updates)
        self.doc.update(updates)
        return True

    async def list_agents_by_credential_vault_id(
        self, vault_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(doc)
            for doc in [self.doc, *self.extra]
            if vault_id in (doc.get("credential_vault_ids") or [])
        ]


class _AssistantRepo:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = docs or []

    async def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
        _ = assistant_id
        return None

    async def list_assistants_by_credential_vault_id(
        self, vault_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(doc)
            for doc in self.docs
            if vault_id in (doc.get("credential_vault_ids") or [])
        ]


async def test_vault_holder_list_uses_reverse_indexes_not_one_read_per_target() -> None:
    vaults = _BindingVaultService()
    agents = _AgentRepo(
        extra=[
            {
                "agent_id": "agent-2",
                "name": "Builder",
                "credential_vault_ids": ["vlt-1", "vlt-2"],
            }
        ]
    )
    agents.doc["credential_vault_ids"] = ["vlt-1"]
    assistants = _AssistantRepo(
        [
            {
                "assistant_id": "assistant-1",
                "display_name": "Researcher",
                "credential_vault_ids": ["vlt-1"],
            }
        ]
    )
    service = CredentialBindingService(
        vault_service=vaults,  # type: ignore[arg-type]
        agent_repo=agents,  # type: ignore[arg-type]
        assistant_repo=assistants,  # type: ignore[arg-type]
    )

    holders = await service.list_vault_bindings(
        UserContext(user_id="admin-1"), "vlt-1"
    )

    assert vaults.authorized == [("admin-1", "vlt-1")]
    assert holders == [
        {
            "target_type": "agent",
            "target_id": "agent-1",
            "target_name": "Reviewer",
            "vault_ids": ["vlt-1"],
        },
        {
            "target_type": "agent",
            "target_id": "agent-2",
            "target_name": "Builder",
            "vault_ids": ["vlt-1", "vlt-2"],
        },
        {
            "target_type": "assistant",
            "target_id": "assistant-1",
            "target_name": "Researcher",
            "vault_ids": ["vlt-1"],
        },
    ]


async def test_admin_binding_is_saved_on_agent_and_user_cannot_influence_it() -> None:
    vaults = _BindingVaultService()
    agents = _AgentRepo()
    service = CredentialBindingService(
        vault_service=vaults,  # type: ignore[arg-type]
        agent_repo=agents,  # type: ignore[arg-type]
        assistant_repo=_AssistantRepo(),  # type: ignore[arg-type]
    )
    admin = UserContext(user_id="admin-a", org_id="org-1", roles=["admin"])

    result = await service.set_agent_binding(
        admin,
        "agent-1",
        [" vlt-b ", "vlt-a", "vlt-b"],
    )

    assert vaults.calls == [
        {
            "user": admin,
            "vault_ids": [" vlt-b ", "vlt-a", "vlt-b"],
            "target_type": "agent",
        }
    ]
    assert agents.updates is not None
    assert agents.updates["credential_vault_ids"] == ["vlt-b", "vlt-a"]
    assert agents.updates["credentials_updated_by"] == "admin-a"
    assert result["vault_ids"] == ["vlt-b", "vlt-a"]


def _bound_service(
    *,
    extra_agents: list[dict[str, Any]] | None = None,
    assistants: list[dict[str, Any]] | None = None,
) -> CredentialBindingService:
    agents = _AgentRepo(extra=extra_agents)
    agents.doc["credential_vault_ids"] = ["vlt-managed"]
    return CredentialBindingService(
        vault_service=_BindingVaultService(),  # type: ignore[arg-type]
        agent_repo=agents,  # type: ignore[arg-type]
        assistant_repo=_AssistantRepo(assistants),  # type: ignore[arg-type]
    )


async def test_bound_vault_refusal_names_the_single_holder_and_the_action() -> None:
    service = _bound_service()

    with pytest.raises(APIError) as blocked:
        await service.ensure_vault_unbound("vlt-managed", action="delete")

    assert blocked.value.code == "VAULT_IN_USE"
    assert blocked.value.status_code == 409
    assert blocked.value.data == {
        "action": "delete",
        "holders": [
            {
                "target_type": "agent",
                "target_id": "agent-1",
                "target_name": "Reviewer",
            }
        ],
    }
    assert blocked.value.message == (
        "Credential Vault is still assigned to agent 'Reviewer'. "
        "Unbind it before deleting the Vault."
    )


async def test_the_refusal_names_the_action_the_operator_asked_for() -> None:
    """Archiving is refused as archiving, not as "archiving or deleting"."""

    with pytest.raises(APIError) as blocked:
        await _bound_service().ensure_vault_unbound("vlt-managed", action="archive")

    assert "before archiving the Vault" in blocked.value.message
    assert "deleting" not in blocked.value.message
    assert blocked.value.data["action"] == "archive"


async def test_every_holder_is_reported_in_one_refusal() -> None:
    """Naming one at a time would make unbinding a retry loop."""

    service = _bound_service(
        extra_agents=[
            {
                "agent_id": "agent-2",
                "name": "releaser-internal",
                "display_meta": {"display_name": "Releaser"},
                "credential_vault_ids": ["vlt-managed"],
            },
            {
                "agent_id": "agent-3",
                "name": "triager-internal",
                "display_meta": {"display_name": "Triager"},
                "credential_vault_ids": ["vlt-managed"],
            },
        ],
        assistants=[
            {
                "assistant_id": "asst-1",
                "display_name": "Desk",
                "credential_vault_ids": ["vlt-managed"],
            }
        ],
    )

    with pytest.raises(APIError) as blocked:
        await service.ensure_vault_unbound("vlt-managed", action="delete")

    holders = blocked.value.data["holders"]
    assert [(h["target_type"], h["target_id"], h["target_name"]) for h in holders] == [
        ("agent", "agent-1", "Reviewer"),
        ("agent", "agent-2", "Releaser"),
        ("agent", "agent-3", "Triager"),
        ("assistant", "asst-1", "Desk"),
    ]
    # The sentence stays readable: a count, a named head, and the remainder.
    assert blocked.value.message == (
        "Credential Vault is still assigned to 4 targets: agent 'Reviewer', "
        "agent 'Releaser', agent 'Triager', and 1 more. "
        "Unbind them before deleting the Vault."
    )


async def test_an_unbound_vault_is_not_refused() -> None:
    agents = _AgentRepo()
    service = CredentialBindingService(
        vault_service=_BindingVaultService(),  # type: ignore[arg-type]
        agent_repo=agents,  # type: ignore[arg-type]
        assistant_repo=_AssistantRepo(),  # type: ignore[arg-type]
    )

    await service.ensure_vault_unbound("vlt-managed", action="delete")


async def test_the_refusal_carries_a_registered_error_spec() -> None:
    """An unregistered code falls back to `unregistered`/`platform`.

    Those defaults are wrong here in a way a caller acts on: this refusal is a
    state conflict the caller can clear itself, so a client triaging by
    `category` and `owner` must not be told the platform owns a Vault it can
    unbind. Registering the code is what makes the envelope say that.
    """

    with pytest.raises(APIError) as blocked:
        await _bound_service().ensure_vault_unbound("vlt-managed", action="delete")

    envelope = blocked.value.to_error_envelope()
    assert envelope["status_code"] == 409
    assert envelope["category"] == "state"
    assert envelope["owner"] == "client"
    # Unbinding is a different request, not this one sent again.
    assert envelope["retryable"] is False


async def test_an_action_the_message_cannot_name_is_a_programming_error() -> None:
    """Better a loud failure here than a refusal that misnames what it refused."""

    with pytest.raises(RuntimeError, match="unknown Vault action"):
        await _bound_service().ensure_vault_unbound("vlt-managed", action="purge")
