"""Credential Vault is an admin resource, independent from user identity."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from astrabox.api.routes import vaults as vault_routes
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.vault_service import VaultService


class _VaultRepo:
    def __init__(self) -> None:
        self.vaults: dict[str, dict[str, Any]] = {}
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
