"""Current preparation subjects locked on a provider's own transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.config.settings import get_settings
from astrabox.persistence.repository.agent_repository import _read as read_agent
from astrabox.persistence.repository.assistant_catalog_repository import _assistants, _record
from astrabox.persistence.repository.assistant_defaults_repository import (
    DEPLOYMENT_SCOPE, _layers, _view as layer_view, lock_defaults_scope,
)
from astrabox.persistence.repository.runtime_profile_repository import _view as runtime_profile_view
from astrabox.persistence.sql.models.agents import Agent, AgentRuntime
from astrabox.persistence.sql.models.assistants import Assistant, AssistantDefaults, AssistantWorkspace
from astrabox.persistence.sql.models.runtime_profiles import RuntimeProfile
from astrabox.persistence.sql.models.sessions import Session
from astrabox.seams.runtime_preparation import PreparationSubject


def subject_conflict() -> APIError:
    return APIError(
        code="PREPARATION_SUBJECT_CONFLICT", status_code=409,
        message="preparation subject, owner, revision or operation is no longer current",
    )


@dataclass(frozen=True)
class PreparationSubjectState:
    definition: dict[str, Any]
    environment: dict[str, Any]
    runtime: dict[str, Any] | None
    layers: list[dict[str, Any]]
    materializer_session: dict[str, Any] | None = None


async def lock_subject(
    connection: AsyncConnection, subject: PreparationSubject,
) -> PreparationSubjectState:
    """Lock RuntimeProfile, definition and operation in that order; refuse a moved reference.

    Assistant defaults precede these locks. Definition writers protect their
    child rows; an exclusive Assistant lock also blocks first workspace insert.
    A materializing workspace's owning Session follows its workspace lock.
    No provider-owned rows may be locked before this function.
    """
    assistant = subject.kind == "assistant"
    table = Assistant.__table__ if assistant else Agent.__table__
    key = table.c.assistant_id if assistant else table.c.agent_id
    if assistant:
        await lock_defaults_scope(connection)
    runtime_profile_name = await connection.scalar(
        select(table.c.runtime_profile_name).where(key == subject.id)
    )
    if not runtime_profile_name:
        raise subject_conflict()
    runtime_profiles = RuntimeProfile.__table__
    environment = (await connection.execute(
        select(runtime_profiles).where(runtime_profiles.c.name == runtime_profile_name)
        .with_for_update(read=True)
    )).mappings().first()
    definition = (await connection.execute(
        select(table).where(key == subject.id).with_for_update(read=not assistant)
    )).mappings().first()
    if (
        environment is None or definition is None
        or definition["runtime_profile_name"] != runtime_profile_name
        or (definition["deleted"] if assistant else definition["state"] != "ACTIVE")
    ):
        raise subject_conflict()
    runtime_table = AssistantWorkspace.__table__ if assistant else AgentRuntime.__table__
    runtime_key = runtime_table.c.assistant_id if assistant else runtime_table.c.agent_id
    environment_name = get_settings().environment_name
    runtime_conditions = [runtime_key == subject.id]
    if not assistant:
        runtime_conditions.append(runtime_table.c.environment_name == environment_name)
    runtime = (await connection.execute(
        select(runtime_table).where(*runtime_conditions).with_for_update(read=True)
    )).mappings().first()
    if runtime is not None and runtime["environment_name"] != environment_name:
        raise subject_conflict()
    materializer_session = None
    if assistant and runtime is not None and runtime["state"] == "MATERIALIZING":
        materializer_session = (await connection.execute(
            select(Session.__table__)
            .where(
                Session.session_id == runtime["provisioning_session_id"],
                Session.environment_name == environment_name,
            )
            .with_for_update(read=True)
        )).mappings().first()
    layers: list[dict[str, Any]] = []
    if assistant:
        row = (await connection.execute(
            _assistants().where(key == subject.id)
        )).mappings().one()
        document = _record(row)
        assert document is not None
        layer = layer_view((await connection.execute(
            _layers().where(AssistantDefaults.__table__.c.scope == DEPLOYMENT_SCOPE)
        )).mappings().first())
        if layer is not None:
            layers.append(layer)
    else:
        if runtime is None:
            raise subject_conflict()
        document = await read_agent(connection, subject.id)
    return PreparationSubjectState(
        definition=document, environment=runtime_profile_view(dict(environment)),
        runtime=dict(runtime) if runtime is not None else None, layers=layers,
        materializer_session=dict(materializer_session) if materializer_session is not None else None,
    )
