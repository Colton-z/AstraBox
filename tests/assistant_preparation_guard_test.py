"""An Assistant's prepared resources commit under the revision its startup resolved.

Starting an Assistant resolves its runtime view, configuration revision
included, and hands a resource preparation provider a guard that recomputes
the revision from the rows it locks before the provider writes. The two must
read the deployment's defaults as the Assistant receives them: an Assistant on
another engine than the default RuntimeProfile's does not inherit the
defaults' Skills, and an Assistant on that engine does.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql import insert

from astrabox.common.utils.errors import APIError
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.runtime.preparation_commit import callback_commit_guard
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.assistant_defaults_repository import (
    DEPLOYMENT_SCOPE,
    AssistantDefaultsRepository,
)
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.runtime_profiles import RuntimeProfile
from astrabox.providers import register_builtin_providers
from astrabox.seams.runtime_preparation import (
    PreparationCommitGuard,
    PreparationSubject,
    ResourcePreparationOperation,
)
from tests import _assistant_rows as rows

pytestmark = pytest.mark.usefixtures("database")

# The Hermes adapter installs as `assistant`, beside `dsh_superagent`.
register_builtin_providers()

_SKILL = "https://github.com/anthropics/skills.git@34040c9c568585f6929bedeaad110ad08f079624#skills/pdf"
_SUBJECT = PreparationSubject(kind="assistant", id="a-1")


async def _runtime_profile(name: str, engine_kind: str) -> None:
    await execute(
        insert(RuntimeProfile.__table__).values(
            name=name, engine_kind=engine_kind, network_type="unrestricted", idle_action="terminate"
        )
    )


async def _view() -> AgentView:
    view = await AgentConfigService(AgentRepository(), RuntimeProfileRepository()).resolve_session_harness(
        {"workspace_ref": {"kind": "assistant", "assistant_id": "a-1"}}
    )
    assert view is not None and view.assistant_revision
    return view


async def _startup_guard() -> PreparationCommitGuard:
    """The guard a startup of ``a-1`` gives the provider, at the revision it resolved."""
    view = await _view()
    return callback_commit_guard(
        operation=ResourcePreparationOperation(subject=_SUBJECT, operation_id="op-1"),
        owner_user_id="u-1", revision=view.assistant_revision, provider="fixture",
        config={}, preparation_id="sb-1",
    )


async def _refusal(guard: PreparationCommitGuard) -> str | None:
    """The code the guard refuses a provider's write with, if it does."""
    async with get_persistence().get_engine().begin() as connection:
        try:
            await guard.validate(connection)
        except APIError as error:
            return error.code
    return None


async def test_an_assistant_off_the_default_engine_commits_under_the_revision_it_started_with() -> None:
    defaults = AssistantDefaultsRepository()
    await _runtime_profile("hermes", "assistant")
    await _runtime_profile("superagent", "dsh_superagent")
    assert await defaults.put_layer(
        DEPLOYMENT_SCOPE, {"runtime_profile_name": "hermes", "skills": [_SKILL]},
        expected_version=None, updated_by="admin",
    )
    await rows.assistant("a-1", runtime_profile_name="superagent", engine_kind="dsh_superagent")
    await rows.workspace(
        "a-1", state="READY", engine_kind="dsh_superagent", current_sandbox_id="sb-1",
        prepared_operation_id="op-1", prepared_operation_provider="fixture",
        prepared_operation_deadline="2999-01-01T00:00:00+00:00",
    )

    started = await _startup_guard()
    assert await _refusal(started) is None, "nothing changed since the Assistant started"

    # The default moves to the Assistant's own RuntimeProfile, so the Assistant
    # now inherits the defaults' Skills: what it was prepared from is stale.
    assert await defaults.put_layer(
        DEPLOYMENT_SCOPE, {"runtime_profile_name": "superagent", "skills": [_SKILL]},
        expected_version=1, updated_by="admin",
    )
    assert await _refusal(started) == "PREPARATION_SUBJECT_CONFLICT"
    restarted = await _startup_guard()
    assert _SKILL in (await _view()).skills
    assert await _refusal(restarted) is None
