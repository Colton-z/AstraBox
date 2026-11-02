"""Isolated delegated identities and shared personal grants; no enterprise integration."""

import json
from pathlib import Path

from fastapi import Request
from sqlalchemy import Boolean, Column, MetaData, Table, Text, insert, select, update

from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.sql.ops import fetch_all, fetch_one, transaction
from astrabox.persistence.sql.statements import insert_if_absent_primary_key
from astrabox.seams.runtime_preparation import (
    PreparationUserActionRequired, RuntimeMaterials, RuntimePreparationProvider,
    SessionUserActionCommand, SessionUserActionState,
)

METADATA = MetaData()
GRANTS = Table("satori_fixture_grants", METADATA,
    Column("principal_id", Text, primary_key=True), Column("agent_id", Text, primary_key=True),
    Column("granted", Boolean, nullable=False), Column("refuse_turn", Boolean, nullable=False, server_default="false"))
RECEIPTS = Table("satori_fixture_turns", METADATA,
    Column("input_id", Text, primary_key=True), Column("principal_id", Text, nullable=False),
    Column("session_id", Text, nullable=False), Column("granted", Boolean, nullable=False))


class DelegatedIdentity:
    is_no_auth_default = False

    def login_url(self, next_url="/"):
        return None

    async def resolve(self, headers):
        credential = str(headers.get("authorization") or "").removeprefix("Bearer ")
        actor = json.loads(Path("/data/satori-fixture.private.json").read_text())["tokens"].get(credential)
        if actor is None:
            raise APIError(code="UNAUTHORIZED", message="Invalid delegated credential", status_code=401)
        # Employee headers and request JSON never override the token's binding.
        return UserContext(**actor)


async def grant(principal, agent):
    row = await fetch_one(select(GRANTS).where(GRANTS.c.principal_id == principal, GRANTS.c.agent_id == agent))
    return bool(row and row["granted"])


class PersonalGrants(RuntimePreparationProvider):
    name = "satori_fixture"

    async def read_user_actions(self, *, context):
        ready = await grant(context.principal_id, context.subject.id)
        return SessionUserActionState(ready=ready,
            user_action=None if ready else {"message": "Authorize this Agent", "url": "https://fixture.invalid/consent"},
            actions=(SessionUserActionCommand(id="revoke" if ready else "authorize", label="Revoke" if ready else "Authorize"),))

    async def perform_user_action(self, *, context, action_id):
        async def write(conn):
            await conn.execute(insert_if_absent_primary_key(conn, GRANTS,
                {"principal_id": context.principal_id, "agent_id": context.subject.id, "granted": False}))
            await conn.execute(update(GRANTS).where(GRANTS.c.principal_id == context.principal_id,
                GRANTS.c.agent_id == context.subject.id).values(granted=action_id == "authorize"))
        await transaction(write)
        return await self.read_user_actions(context=context)

    async def prepare_session(self, *, context):
        if not await grant(context.principal_id, context.subject.id):
            raise PreparationUserActionRequired(message="Authorize this Agent")
        return RuntimeMaterials()

    async def prepare_turn(self, *, context):
        row = await SessionRepository().get_owned_session(context.session_id, context.principal_id)
        allowed = await grant(context.principal_id, row["agent_id"])
        async def write(conn):
            await conn.execute(insert_if_absent_primary_key(conn, RECEIPTS,
                {"input_id": context.dispatch_attempt_id, "principal_id": context.principal_id,
                 "session_id": context.session_id, "granted": allowed}))
            refuse = (await conn.execute(select(GRANTS.c.refuse_turn).where(
                GRANTS.c.principal_id == context.principal_id, GRANTS.c.agent_id == row["agent_id"]))).scalar_one()
            return refuse
        refuse = await transaction(write)
        if not allowed:
            raise PreparationUserActionRequired(message="Authorization was revoked")
        if refuse:
            raise RuntimeError("Controlled preparation refusal")
        return RuntimeMaterials()


def register_routes(app):
    @app.get("/api/v1/admin/satori-fixture")
    async def state():
        return success_response({"grants": await fetch_all(select(GRANTS)), "turns": await fetch_all(select(RECEIPTS))})

    @app.post("/api/v1/admin/satori-fixture/refuse/{principal}/{agent}")
    async def refuse(principal: str, agent: str):
        async def write(conn):
            await conn.execute(update(GRANTS).where(GRANTS.c.principal_id == principal,
                GRANTS.c.agent_id == agent).values(refuse_turn=True))
        await transaction(write)
        return success_response({"armed": True})
