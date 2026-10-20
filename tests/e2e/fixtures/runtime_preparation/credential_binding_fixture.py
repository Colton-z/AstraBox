"""Persisted preparation policy and real egress binding for one live journey."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from sqlalchemy import Boolean, Column, JSON, MetaData, Table, Text, select

from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.user_context import get_asserted_user_context
from astrabox.core.service.orchestrator.agent_access import is_platform_admin
from astrabox.core.service.orchestrator.engine.provisioning import model_credential_plan
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.runtime.config_resolver import RuntimeConfigResolver
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider
from astrabox.seams.egress_credentials import ModelEgressCredentialSubstitution
from astrabox.seams.model import ModelRequestContext, model_endpoint_for_name
from astrabox.seams.runtime_preparation import (
    PreparationUserActionRequired, RuntimeFile, RuntimeMaterials,
    RuntimePreparationPlan, SessionUserActionState,
)
from astrabox.seams.sandbox import sandbox_for_name

METADATA = MetaData()
POLICIES = Table(
    "runtime_preparation_binding_policies", METADATA,
    Column("environment_name", Text, primary_key=True),
    Column("session_bound", Boolean, nullable=False),
    Column("users", JSON, nullable=False),
    Column("refuse_binding", Boolean, nullable=False),
    Column("startup_environment", Boolean, nullable=False),
    Column("console_base_url", Text, nullable=False, server_default=""),
)
RECEIPTS = Table(
    "runtime_preparation_binding_receipts", METADATA,
    Column("session_id", Text, primary_key=True),
    Column("principal_id", Text, nullable=False),
    Column("sandbox_id", Text, nullable=False),
    Column("placeholder", Text, nullable=False),
    Column("key_digest", Text, nullable=False),
    Column("engine_initialized", Boolean, nullable=False),
)


class BindingSandboxProvider(OpenSandboxSandboxProvider):
    name = "binding_fixture"
    is_default = False

    async def create_sandbox(self, spec):
        return await super().create_sandbox(replace(spec, credential_proxy_enabled=True))


async def policy(environment):
    async with get_persistence().get_engine().connect() as connection:
        row = (await connection.execute(select(POLICIES).where(POLICIES.c.environment_name == environment))).mappings().one()
    return dict(row)


async def plan(config):
    row = await policy(config["environment_name"])
    return RuntimePreparationPlan(session_bound=row["session_bound"], model_credential_delivery="provider")


async def prerequisites(context):
    if context.subject.kind != "agent":
        return None
    agent = await AgentRepository().get_agent(context.subject.id)
    if agent is None:
        return None
    environment = await EnvironmentRepository().get_any_by_name(agent["environment_name"])
    if environment is None or environment["sandbox_backend"] != BindingSandboxProvider.name:
        return None
    row = await policy(environment["name"])
    if context.principal_id not in row["users"]:
        return SessionUserActionState(ready=False, user_action={"message": "Authorize this identity before binding the model"})
    return SessionUserActionState(ready=True)


async def bind(context):
    row = await policy(context.config["environment_name"])
    if context.principal_id not in row["users"]:
        raise PreparationUserActionRequired(message="Authorize this identity before binding the model")
    if row["refuse_binding"]:
        raise RuntimeError("fixture refused binding")
    assert context.model_credential_placeholder
    access = RuntimeConfigResolver(load_astrabox_settings()).resolve_model_access(context.config["model_config"])
    key = await model_endpoint_for_name(access.endpoint_provider).ensure_session_credential(
        context=ModelRequestContext(conversation_id=context.session_id, user_id=context.principal_id),
    )
    assert key
    request = get_engine_adapter(context.config["engine_kind"]).sandbox_request(
        template=SimpleNamespace(**context.config), model_access=access,
    )
    credential = replace(request.credential, access=replace(access, credential=key))
    protected = model_credential_plan(credential, substitutions=(ModelEgressCredentialSubstitution(
        name="binding-fixture", secret_value=key, placeholder=context.model_credential_placeholder,
    ),))
    await sandbox_for_name(context.config["sandbox_backend"]).apply_credential_vault(context.sandbox, vault_write=protected)
    async with get_persistence().get_engine().begin() as connection:
        await connection.execute(RECEIPTS.delete().where(RECEIPTS.c.session_id == context.session_id))
        await connection.execute(RECEIPTS.insert().values(
            session_id=context.session_id, principal_id=context.principal_id,
            sandbox_id=context.sandbox_id, placeholder=context.model_credential_placeholder,
            key_digest=hashlib.sha256(key.encode()).hexdigest(), engine_initialized=context.engine_initialized,
        ))
    environment = {}
    if row["startup_environment"]:
        assert row["console_base_url"].startswith(("http://", "https://"))
        environment = {
            "ASTRABOX_SESSION_URL": row["console_base_url"].rstrip("/") + "/sessions/" + context.session_id,
            "RUNTIME_IDENTITY_SESSION_ID": context.session_id,
            "BINDING_FIXTURE_OWNER": context.principal_id,
        }
    if not row["session_bound"]:
        return RuntimeMaterials(environment=environment)
    material = RuntimeFile("agent-token.json", json.dumps({
        "session_id": context.session_id, "principal_id": context.principal_id,
        "sandbox_id": context.sandbox_id, "phase": "startup",
    }).encode())
    return RuntimeMaterials(files=(material,), environment={
        **environment,
        **({} if context.engine_initialized else {"ASTRABOX_CFUSE_AGENT_TOKEN_FILE": context.file_path(material.name)}),
    })


def register_routes(app: FastAPI):
    @app.put("/api/v1/runtime-preparation-fixture/binding-policies/{environment}")
    async def put_policy(environment: str, payload: dict):
        user = get_asserted_user_context()
        if user is None or not is_platform_admin(user.roles):
            raise APIError(code="FORBIDDEN", message="fixture requires a platform administrator", status_code=403)
        async with get_persistence().get_engine().begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.execute(POLICIES.delete().where(POLICIES.c.environment_name == environment))
            await connection.execute(POLICIES.insert().values(environment_name=environment, **payload))
        return success_response({"configured": True})
