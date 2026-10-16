"""An Environment's credentials are sealed in the secret store; its row names them.

`provider_access.api_key` and `tracing.auth_token` are reusable credentials. The
Environment row holds only the key each value has in the secret store, every
client-facing read shows a mask, and a write that sends the mask back keeps the
stored value. Only the runtime's read (`get_with_secrets`) returns the values.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from sqlalchemy import select

import astrabox.providers.secret_store  # noqa: F401 - registers the local secret store
import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 — registers the claude_code engine (engine_kind enum validation)
import astrabox.providers.open_sandbox.sandbox  # noqa: F401 — registers the sandbox provider (Environment capability validation)
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.persistence.repository.environment_repository import (
    SECRET_MASK,
    EnvironmentRepository,
)
from astrabox.persistence.sql import fetch_all, fetch_one
from astrabox.persistence.sql.models.environments import Environment
from astrabox.persistence.sql.models.vaults import Secret

pytestmark = pytest.mark.usefixtures("database")

_USER = UserContext(user_id="admin", roles=["admin"])

_TRACING = {
    "enabled": True,
    "endpoint": "https://collector.example.com/api/public/otel",
    "signals": ["traces", "metrics"],
}


@pytest.fixture(autouse=True)
def _sealed_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"s" * 32).decode("ascii")
    )


def _svc() -> AgentConfigService:
    return AgentConfigService(
        agent_repo=object(),  # type: ignore[arg-type]  # unused on the environment path
        environment_repo=EnvironmentRepository(),
        assistant_repo=object(),  # type: ignore[arg-type]  # unused
    )


async def _row(name: str) -> dict[str, Any]:
    row = await fetch_one(
        select(Environment.__table__).where(Environment.__table__.c.name == name)
    )
    assert row is not None
    return row


async def _sealed(name: str) -> dict[str, str]:
    """The secret store's rows for this Environment, by key."""
    secrets = Secret.__table__
    rows = await fetch_all(select(secrets).where(secrets.c.scope == f"environment/{name}"))
    return {str(row["key"]): str(row["sealed"]) for row in rows}


async def test_the_row_holds_references_and_every_read_masks_the_values() -> None:
    svc = _svc()
    written = await svc.upsert_environment_config(_USER, "e", {
        "engine_kind": "claude_code",
        "provider_access": {"base_url": "http://gateway", "api_key": "sk-REAL"},
        "tracing": {**_TRACING, "auth_token": "Basic REAL"},
    })

    row = await _row("e")
    assert "sk-REAL" not in str(row) and "Basic REAL" not in str(row)
    sealed = await _sealed("e")
    assert set(sealed) == {row["provider_api_key_ref"], row["tracing_auth_token_ref"]}
    assert "sk-REAL" not in str(sealed) and "Basic REAL" not in str(sealed)

    listed = (await svc.list_environment_configs(_USER))[0]
    fetched = await svc.get_environment("e")
    for document in (written, listed, fetched):
        assert document is not None
        assert document["provider_access"] == {"base_url": "http://gateway", "api_key": SECRET_MASK}
        assert document["tracing"]["auth_token"] == SECRET_MASK

    resolved = await EnvironmentRepository().get_with_secrets("e")
    assert resolved is not None
    assert resolved["provider_access"]["api_key"] == "sk-REAL"
    assert resolved["tracing"]["auth_token"] == "Basic REAL"
