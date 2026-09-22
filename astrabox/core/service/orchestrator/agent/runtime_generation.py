"""Derive compatibility for an Agent's base boxes and prepared engine slots.

Shared base boxes and engine slots have different lifetimes. Whole-box pools
publish an initialized engine, so their recipe includes its startup configuration.
Changing that recipe retires unclaimed inventory, never a Session-owned box.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    required_network_host,
    resolve_network_policy,
    resolve_runtime_template_name,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    sandbox_mcp_egress_hosts,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import (
    get_template_plugin_repos,
    plugin_repo_egress_hosts,
)
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    resolve_sandbox_permission_level,
    resolve_sandbox_tenancy,
)
from astrabox.seams.sandbox import (
    SANDBOX_TENANCY_CONVERSATION,
    sandbox_for_name,
    sandbox_for_template,
)


def _value(subject: Any, key: str, default: Any = None) -> Any:
    if isinstance(subject, Mapping):
        return subject.get(key, default)
    return getattr(subject, key, default)


def agent_runtime_owner_id(agent_id: str) -> str:
    """Return the platform owner of one Agent-shared physical sandbox."""

    target = str(agent_id or "").strip()
    if not target:
        raise ValueError("Agent runtime owner requires an Agent id")
    return f"agent-runtime:{target}"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json", by_alias=True, exclude_none=True))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    return str(value)


def environment_credentials_generation_contract(
    credentials: list[Any],
) -> list[dict[str, Any]]:
    """Return the non-secret credential policy frozen into a box create."""

    return [
        {
            "credential_id": str(item.credential_id),
            "secret_name": str(item.secret_name),
            "networking": _jsonable(item.networking),
            "injection_location": _jsonable(item.injection_location),
            "allow_insecure_http": bool(item.allow_insecure_http),
            "allowed_requests": _jsonable(item.allowed_requests),
        }
        for item in sorted(
            credentials,
            key=lambda credential: (
                str(credential.secret_name),
                str(credential.credential_id),
            ),
        )
    ]


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def runtime_generations(
    template: Any, *, runtime_manager: Any
) -> tuple[str, str]:
    """Return the engine generation and the base-box preparation fingerprint."""

    agent_id = str(_value(template, "agent_id", "") or "").strip()
    engine_kind = str(_value(template, "engine_kind", "") or "").strip()
    if not agent_id or not engine_kind:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="runtime preparation requires an Agent id and engine kind",
            status_code=500,
        )
    model_access = runtime_manager.resolve_model_access(
        dict(_value(template, "model_config", {}) or {})
    )
    adapter = get_engine_adapter(engine_kind)
    request = adapter.sandbox_request(template=template, model_access=model_access)
    model_host = (
        required_network_host(str(model_access.base_url), label="model endpoint")
        if str(model_access.base_url or "").strip()
        else ""
    )
    network_policy = resolve_network_policy(
        template,
        required_hosts=(
            *tuple(request.required_network_hosts),
            *([model_host] if model_host else []),
            *plugin_repo_egress_hosts(template),
        ),
        mcp_hosts=sandbox_mcp_egress_hosts(_value(template, "mcp_servers", {})),
    )
    settings = runtime_manager.deployment_settings
    vault_ids = [
        str(item).strip()
        for item in (_value(template, "credential_vault_ids", []) or [])
        if str(item or "").strip()
    ]
    environment_credentials: list[Any] = []
    if vault_ids:
        from astrabox.core.service.orchestrator.vault_service import VaultService

        environment_credentials = await VaultService().resolve_env_credentials(
            vault_ids,
            placeholder_context=f"runtime-generation:{agent_id}",
        )
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "prewarm_revision": _value(template, "prewarm_revision"),
        "environment_updated_at": str(
            _value(template, "environment_updated_at", "") or ""
        ),
        "image": resolve_runtime_template_name(template),
        "engine_kind": engine_kind,
        "sandbox_backend": str(sandbox_for_template(template).name),
        "sandbox_tenancy": resolve_sandbox_tenancy(template),
        "permission_level": resolve_sandbox_permission_level(template),
        "network_policy": network_policy,
        "box": {
            "entrypoint": tuple(request.entrypoint),
            "env": dict(request.env),
            "credential_env_var": request.credential_env_var,
            "plan_identity": request.plan_identity,
            "cwd": request.cwd,
            "cwd_env_var": request.cwd_env_var,
            "publish_ports": tuple(request.publish_ports),
            "wait_for_inbox_service_port": request.wait_for_inbox_service_port,
            "required_network_hosts": tuple(request.required_network_hosts),
        },
        "model": {
            "configuration": dict(model_access.configuration),
            "base_url": model_access.base_url,
            "model_name": model_access.model_name,
            "credential_kind": model_access.credential_kind,
            "endpoint_provider": model_access.endpoint_provider,
            "header": request.credential.header,
            "request_methods": tuple(request.credential.request_methods),
            "request_paths": tuple(request.credential.request_paths),
        },
        "agent_program": {
            "system": _value(template, "system"),
            "mcp_servers": _value(template, "mcp_servers"),
            "skills": list(_value(template, "skills", []) or []),
            "engine_options": _value(template, "engine_options"),
            "default_repo": _value(template, "default_repo"),
            "plugin_repos": get_template_plugin_repos(template),
            "credential_vault_ids": vault_ids,
            "environment_credentials": environment_credentials_generation_contract(
                environment_credentials
            ),
        },
        "platform": {
            "credential_vault_enabled": bool(
                getattr(settings, "sandbox_credential_vault_enabled", False)
            ),
            "mcp_proxy_base_url": str(getattr(settings, "mcp_proxy_base_url", "") or "").strip(),
        },
    }
    # Shared base boxes cache Skills and Plugins before a separate engine slot
    # is prepared. Their recipe excludes injectable engine configuration.
    box_payload = {
        **{key: value for key, value in payload.items() if key != "environment_updated_at"},
        "model": {
            key: value
            for key, value in payload["model"].items()
            if key not in {"configuration", "model_name"}
        },
        "agent_program": {
            key: payload["agent_program"][key]
            for key in (
                "skills", "plugin_repos", "credential_vault_ids", "environment_credentials"
            )
        },
    }
    # A whole-box pool publishes an initialized engine, so its immutable
    # preparer consumes the complete runtime recipe, not only downloaded files.
    if resolve_sandbox_tenancy(template) == SANDBOX_TENANCY_CONVERSATION:
        box_payload = payload
    return _fingerprint(payload), _fingerprint(box_payload)


async def reconcile_runtime_generation(
    template: Any,
    *,
    runtime_manager: Any,
    agent_repo: Any | None = None,
) -> str:
    """Publish the generation and fence any older prepared/shared capacity."""

    from astrabox.core.service.orchestrator.agent.prepared_slots import (
        PREPARED_SLOT_FIELD,
        retire_prepared_runtime,
    )
    from astrabox.persistence.repository.agent_repository import AgentRepository

    agent_id = str(_value(template, "agent_id", "") or "").strip()
    generation, sandbox_generation = await runtime_generations(
        template, runtime_manager=runtime_manager
    )
    if isinstance(template, dict):
        template["runtime_generation"] = generation
        template["sandbox_generation"] = sandbox_generation
    else:
        template.runtime_generation = generation
        template.sandbox_generation = sandbox_generation
    repo = agent_repo if agent_repo is not None else AgentRepository()

    def _publish_pool_epoch(epoch: str) -> None:
        if isinstance(template, dict):
            template["client_pool_epoch"] = epoch
        else:
            template.client_pool_epoch = epoch

    async def _retire_empty_stale_resident() -> None:
        current = await repo.get_agent(agent_id)
        if not isinstance(current, dict):
            return
        sandbox_id = str(current.get("sandbox_id") or "").strip()
        resident_generation = str(
            current.get("_resident_sandbox_generation") or ""
        ).strip()
        if not sandbox_id or resident_generation == sandbox_generation:
            return
        backend = str(current.get("sandbox_backend") or "").strip()
        if not backend:
            raise APIError(
                code="AGENT_RUNTIME_GENERATION_CONFLICT",
                message=(
                    f"Agent {agent_id!r} has a stale resident sandbox without its "
                    "provider identity"
                ),
                status_code=500,
            )
        occupied = await runtime_manager.agent_box_has_other_occupants(
            sandbox_id, excluding=""
        )
        if occupied:
            # Existing Sessions keep this box. New placements see the stale
            # resident generation and replace the Agent pointer with a current
            # candidate; the last existing Session then owns old-box cleanup.
            return
        destruction = await sandbox_for_name(backend).confirm_destroyed(sandbox_id)
        if not destruction.confirmed:
            raise APIError(
                code="SANDBOX_CLEANUP_UNCONFIRMED",
                message=(
                    f"stale Agent sandbox {sandbox_id!r} could not be retired: "
                    f"{destruction.detail}"
                ),
                status_code=502,
            )
        await repo.compare_and_update_agent(
            agent_id,
            expected={
                "sandbox_id": sandbox_id,
                "sandbox_backend": current.get("sandbox_backend"),
                "_resident_sandbox_generation": (
                    current.get("_resident_sandbox_generation")
                    if "_resident_sandbox_generation" in current
                    else {"$exists": False}
                ),
            },
            updates={
                "sandbox_id": None,
                "sandbox_backend": None,
                "_resident_sandbox_generation": None,
            },
        )

    for _ in range(3):
        row = await repo.get_agent(agent_id)
        if not isinstance(row, dict):
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"Agent {agent_id!r} disappeared during runtime reconciliation",
                status_code=404,
            )
        stored = str(row.get("_runtime_generation") or "").strip()
        epoch = str(row.get("_client_pool_epoch") or "").strip()
        same_box_recipe = row.get("_sandbox_generation") == sandbox_generation
        if stored == generation and same_box_recipe and epoch:
            _publish_pool_epoch(epoch)
            await _retire_empty_stale_resident()
            return generation
        await retire_prepared_runtime(
            agent_id,
            reason="Agent runtime generation changed",
            agent_repo=repo,
        )
        current = await repo.get_agent(agent_id)
        if not isinstance(current, dict):
            continue

        def _expected(key: str) -> Any:
            return current.get(key) if key in current else {"$exists": False}

        # A retired namespace cannot be restarted, even when its recipe is
        # selected again. Persist one lifetime before any supplier is started;
        # injectable engine edits and process restarts retain that lifetime.
        current_epoch = str(current.get("_client_pool_epoch") or "").strip()
        epoch = (
            current_epoch
            if current_epoch and current.get("_sandbox_generation") == sandbox_generation
            else uuid.uuid4().hex
        )
        updated = await repo.compare_and_update_agent(
            agent_id,
            expected={
                "_runtime_generation": _expected("_runtime_generation"),
                "sandbox_id": _expected("sandbox_id"),
                "sandbox_backend": _expected("sandbox_backend"),
                "_sandbox_generation": _expected("_sandbox_generation"),
                "_client_pool_epoch": _expected("_client_pool_epoch"),
                "_resident_sandbox_generation": _expected(
                    "_resident_sandbox_generation"
                ),
                PREPARED_SLOT_FIELD: _expected(PREPARED_SLOT_FIELD),
            },
            updates={
                "_runtime_generation": generation,
                "_sandbox_generation": sandbox_generation,
                "_client_pool_epoch": epoch,
            },
        )
        if updated:
            _publish_pool_epoch(epoch)
            await _retire_empty_stale_resident()
            return generation
    raise APIError(
        code="AGENT_RUNTIME_GENERATION_CONFLICT",
        message=(
            f"Agent {agent_id!r} changed while its runtime generation was being "
            "published; retry with the current Agent definition"
        ),
        status_code=409,
    )


__all__ = [
    "environment_credentials_generation_contract",
    "reconcile_runtime_generation",
    "runtime_generations",
]
