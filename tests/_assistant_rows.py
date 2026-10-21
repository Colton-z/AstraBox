"""Assistant, workspace and referenced rows written directly to the test database.

For tests that start from a stored state rather than build it through the
services. The RuntimeProfile row carries only the columns its table requires:
its own domain owns the rest of that record.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    assistant_profile_marker_key,
)
from astrabox.persistence.repository.assistant_workspace_repository import AssistantWorkspaceRepository
from astrabox.persistence.sql import execute, from_iso
from astrabox.persistence.sql.models.assistants import (
    Assistant,
    AssistantProfile,
    AssistantWorkspace,
)
from astrabox.persistence.sql.models.runtime_profiles import RuntimeProfile

_TIMES = frozenset(
    {
        "created_at",
        "updated_at",
        "credentials_updated_at",
        "current_sandbox_expires_at",
        "hibernated_at",
        "profile_refresh_started_at",
        "prepared_operation_deadline",
    }
)


def _typed(values: dict[str, Any]) -> dict[str, Any]:
    return {key: from_iso(value) if key in _TIMES else value for key, value in values.items()}


async def environment(name: str) -> None:
    await execute(
        pg_insert(RuntimeProfile.__table__)
        .values(
            name=name, engine_kind="assistant", network_type="unrestricted", idle_action="terminate"
        )
        .on_conflict_do_nothing()
    )


async def assistant(
    assistant_id: str,
    *,
    owner_id: str = "u-1",
    runtime_profile_name: str = "env-1",
    engine_kind: str = "assistant",
    **values: Any,
) -> None:
    await environment(runtime_profile_name)
    await execute(
        insert(Assistant.__table__).values(
            assistant_id=assistant_id,
            owner_id=owner_id,
            display_name=values.pop("display_name", assistant_id),
            engine_kind=engine_kind,
            runtime_profile_name=runtime_profile_name,
            **_typed(values),
        )
    )


async def workspace(
    assistant_id: str,
    *,
    state: str,
    created_by_user_id: str = "u-1",
    engine_kind: str = "assistant",
    profiles: dict[str, str] | None = None,
    **values: Any,
) -> None:
    """The Assistant's workspace row; ``profiles`` maps each user to the box their profile is in."""
    values.setdefault("environment_name", AssistantWorkspaceRepository().environment_name)
    await execute(
        insert(AssistantWorkspace.__table__).values(
            assistant_id=assistant_id,
            created_by_user_id=created_by_user_id,
            engine_kind=engine_kind,
            state=state,
            **_typed(values),
        )
    )
    for user_id, sandbox_id in (profiles or {}).items():
        await execute(
            insert(AssistantProfile.__table__).values(
                assistant_id=assistant_id,
                marker_key=assistant_profile_marker_key(user_id=user_id, assistant_id=assistant_id),
                user_id=user_id,
                sandbox_id=sandbox_id,
                configuration_revision="rev-1",
            )
        )
