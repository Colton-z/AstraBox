"""Assistants, their workspaces and the Assistant defaults on the relational store.

Each case starts from rows in the test database and drives the real services
and repositories, then reads the rows back. The concurrent cases hold every
writer until all of them have read, so each computes its write from the same
state, which is the interleaving a lost update needs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.assistant.assistant_defaults import (
    AssistantDefaultsService,
)
from astrabox.core.service.orchestrator.assistant.assistant_service import AssistantService
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
)
from astrabox.core.service.orchestrator.runtime.storage._identity import ensure_workspace_id
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)
from astrabox.persistence.repository.assistant_defaults_repository import (
    DEPLOYMENT_SCOPE,
    AssistantDefaultsRepository,
)
from astrabox.providers import register_builtin_providers
from tests import _assistant_rows as rows

pytestmark = pytest.mark.usefixtures("database")

# The Assistant program must be installed for its engine to be valid.
register_builtin_providers()

_OWNER = UserContext(user_id="u-1", org_id="default", roles=[])


class _Environments:
    """One unrestricted Environment that runs Assistants, for the authoring checks."""

    async def get_environment(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "enabled": True,
            "engine_kind": "assistant",
            "networking": {"type": "unrestricted"},
        }

    async def get_any_by_name(self, name: str) -> dict[str, Any]:
        return {**await self.get_environment(name), "updated_at": "2026-01-01T00:00:00+00:00"}

    async def list_all(self) -> list[dict[str, Any]]:
        return []


class _ReadTogether:
    """Hold each of the first ``readers`` reads until all of them have read."""

    def __init__(self, readers: int) -> None:
        self._readers = readers
        self._arrived = 0
        self._all_read = asyncio.Event()

    async def hold(self, value: Any) -> Any:
        self._arrived += 1
        if self._arrived >= self._readers:
            self._all_read.set()
        await asyncio.wait_for(self._all_read.wait(), 10)
        return value


def _assistant_service(catalog: AssistantCatalogRepository | None = None) -> AssistantService:
    return AssistantService(
        agent_config=_Environments(),
        session_lifecycle=None,
        catalog_repo=catalog or AssistantCatalogRepository(),
        workspace_service=AssistantWorkspaceService(),
        spawn_background_task=lambda coroutine, **_: coroutine.close(),
    )


async def _outcome(call: Any) -> Any:
    try:
        return await call
    except APIError as exc:
        return exc


# ── lost updates ─────────────────────────────────────────────────────────────


async def test_concurrent_edits_of_one_assistant_cannot_overwrite_each_other() -> None:
    # Six owners' tabs read the same Assistant and save different prompts. A
    # read-then-write that does not check what it read lets every save land,
    # each silently replacing the one before.
    await rows.assistant("a-1", owner_id="u-1")
    readers = _ReadTogether(6)

    class _Catalog(AssistantCatalogRepository):
        async def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
            return await readers.hold(await super().get_assistant(assistant_id))

    service = _assistant_service(_Catalog())
    outcomes = await asyncio.gather(
        *(
            _outcome(service.update_assistant(_OWNER, "a-1", {"system": f"prompt-{index}"}))
            for index in range(6)
        )
    )

    saved = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, APIError)]
    assert len(saved) == 1
    assert {(error.code, error.status_code) for error in refused} == {
        ("ASSISTANT_VERSION_CONFLICT", 409)
    }
    stored = await AssistantCatalogRepository().get_assistant("a-1")
    assert stored is not None
    assert stored["system"] == saved[0]["system"]
    assert stored["version"] == 2


async def test_concurrent_saves_of_the_assistant_defaults_cannot_overwrite_each_other() -> None:
    layers = AssistantDefaultsRepository()
    assert await layers.put_layer(
        DEPLOYMENT_SCOPE, {"instructions": "first"}, expected_version=None, updated_by="admin",
    )
    readers = _ReadTogether(5)

    class _Layers(AssistantDefaultsRepository):
        async def get_layer(self, scope: str) -> dict[str, Any] | None:
            return await readers.hold(await super().get_layer(scope))

    service = AssistantDefaultsService(
        repo=_Layers(), environments=_Environments(), assistant_service=SimpleNamespace(),  # type: ignore[arg-type]
    )
    admin = UserContext(user_id="admin", roles=["admin"])
    outcomes = await asyncio.gather(
        *(_outcome(service.put(admin, {"instructions": f"save-{index}"})) for index in range(5))
    )

    saved = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, APIError)]
    assert len(saved) == 1
    assert {(error.code, error.status_code) for error in refused} == {
        ("ASSISTANT_DEFAULTS_VERSION_CONFLICT", 409)
    }
    stored = await layers.get_layer(DEPLOYMENT_SCOPE)
    assert stored is not None
    assert stored["instructions"] == saved[0]["instructions"]
    assert stored["version"] == 2


# ── the durable workspace id ─────────────────────────────────────────────────


async def test_concurrent_first_mounts_share_one_workspace_id() -> None:
    # Pool replenishment and a cold start can mount an Assistant's first box at
    # once. Two ids would put the files of one box under a root the next box
    # never mounts.
    await rows.assistant("a-1", updated_at="2026-01-01T00:00:00+00:00")

    minted = await asyncio.gather(
        *(ensure_workspace_id(subject_kind="assistant_runtime", assistant_id="a-1") for _ in range(8))
    )

    assert len(set(minted)) == 1
    stored = await AssistantCatalogRepository().get_assistant("a-1")
    assert stored is not None
    assert stored["workspace_id"] == minted[0]
    # The id is the platform's bookkeeping, not an edit of the Assistant.
    assert (stored["updated_at"], stored["version"]) == ("2026-01-01T00:00:00+00:00", 1)


# ── the primary Assistant ────────────────────────────────────────────────────


async def test_concurrent_first_opens_create_one_primary_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await rows.environment("env-1")
    assert await AssistantDefaultsRepository().put_layer(
        DEPLOYMENT_SCOPE, {"environment_name": "env-1"}, expected_version=None, updated_by="admin",
    )
    monkeypatch.setattr(
        "astrabox.persistence.repository.environment_repository.EnvironmentRepository",
        lambda: _Environments(),
    )
    service = _assistant_service()

    with patch.object(service, "wake_workspace", AsyncMock()):
        opened = await asyncio.gather(
            *(
                service.ensure_primary_assistant(_OWNER, display_name="Mine", timezone=None)
                for _ in range(5)
            )
        )

    assert len({assistant["assistant_id"] for assistant, _created in opened}) == 1
    assert [created for _assistant, created in opened].count(True) == 1
    owned = await AssistantCatalogRepository().list_owner_assistants("u-1")
    assert [(assistant["assistant_id"], assistant["primary"]) for assistant, _ in owned] == [
        (opened[0][0]["assistant_id"], True)
    ]


# ── the materializer's startup generation ────────────────────────────────────


async def test_the_startup_generation_fences_publication_failure_and_takeover() -> None:
    # A command of the same Session that started later took a newer generation.
    # What the earlier command still does — publish, fail, or be taken over as
    # stalled — must not land on the newer command's claim.
    await rows.assistant("a-1")
    await rows.workspace("a-1", state="MATERIALIZING", provisioning_session_id="session")
    service = AssistantWorkspaceService()
    owner = {"user_id": "u-1", "assistant_id": "a-1", "provisioning_session_id": "session"}

    assert await service.claim_materialization_generation(
        **owner, observed_generation=None, sandbox_generation="generation-1",
    )
    assert await service.claim_materialization_generation(
        **owner, observed_generation="generation-1", sandbox_generation="generation-2",
    )
    assert not await service.claim_materialization_generation(
        **owner, observed_generation=None, sandbox_generation="stale-generation",
    )
    assert not await service.mark_materialization_failed(
        **owner, provisioning_sandbox_generation="generation-1", failure_phase="late_failure",
    )
    assert not await service.claim_stalled_materialization(
        **owner, provisioning_sandbox_generation="generation-1", last_error="late_cleanup",
    )
    ready = {**owner, "sandbox_id": "box-2", "configuration_revision": "rev-1"}
    with pytest.raises(APIError) as stale:
        await service.mark_ready(**ready, provisioning_sandbox_generation="generation-1")
    assert stale.value.code == "ASSISTANT_WORKSPACE_READY_CONFLICT"

    assert await service.mark_ready(**ready, provisioning_sandbox_generation="generation-2")
    current = await service.get_workspace(user_id="u-1", assistant_id="a-1")
    assert current is not None
    assert (current["state"], current["current_sandbox_id"]) == ("READY", "box-2")
    assert current["provisioning_sandbox_generation"] is None
