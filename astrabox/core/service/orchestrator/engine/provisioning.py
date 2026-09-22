"""Sandbox provisioning as a contract: the adapter declares, the platform composes.

Every other part of the engine seam is declared. Capabilities are a dataclass
the registry validates, optional powers are protocols the platform asks about,
and an engine's own knobs are a schema the console renders. Provisioning is
declared the same way: an adapter states what a box must be — its entrypoint,
its environment, where its model credential rides, which gateway paths the
sidecar may inject into — and :func:`provision_engine_sandbox` resolves the
backend, plans the conversation identity, composes the vault delivery, builds
the create spec, creates and tracks, in that order. An adapter cannot forget a
step it never performs.

Three requirements are implicit in the running box and explicit here, because
each is missed the same way: the box reaches READY and then fails somewhere
else entirely.

* The create spec's ``entrypoint=None`` default resolves to the Claude agent
  image's boot script, so another image would exit 127 at boot. Every request
  therefore declares an entrypoint.
* Turn dispatch requires ``runtime_identity`` on the session row before it
  even consults the runtime registry; without it a session reaches READY and
  every turn dies reporting an attach failure.
* Networking and protected credential delivery are independent create inputs.
  Every backend must enforce the Environment's reachability and, when enabled,
  translate the same provider-neutral credential plan without letting either
  input widen, narrow, enable, or disable the other.

What stays with the adapter is engine semantics: which base URL its wire needs
(Hermes rejects an Anthropic-shaped one; the harness takes the gateway as-is),
what its image calls the credential variable, and what its own error
vocabulary says when an environment supplies neither.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.persistence.repository.session_repository import (
    SessionRepository,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    required_network_host,
    resolve_network_policy,
    resolve_runtime_template_name,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
)
from astrabox.core.service.orchestrator.runtime.storage import (
    WORKSPACE_ID_FIELD,
    claim_workspace_id,
    mint_workspace_id,
)
from astrabox.core.service.orchestrator.runtime.storage.mounts import (
    bind_prepared_workspace,
    create_sandbox_with_storage,
)
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    resolve_sandbox_permission_level,
    resolve_sandbox_tenancy,
)
from astrabox.core.service.orchestrator.runtime.mcp_credentials import (
    mcp_credential_refresher,
    resolve_agent_mcp_credential_plan,
    resolve_mcp_credential_plan,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    sandbox_mcp_egress_hosts,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import (
    plugin_repo_egress_hosts,
)
from astrabox.providers.sandbox_image import SANDBOX_SELF_DESCRIPTION
from astrabox.seams.egress_credentials import (
    EGRESS_HELD_PLACEHOLDER,
    ModelEgressCredential,
    ModelEgressCredentialSubstitution,
    SandboxEgressCredentialPlan,
    credential_request_path,
    merge_credential_plans,
    workload_placeholder_context,
    workload_credential_name,
    workload_model_placeholder,
)
from astrabox.seams.sandbox import (
    SANDBOX_PERMISSION_LEVEL_ADVANCED,
    SANDBOX_PERMISSION_LEVEL_DEFAULT,
    SANDBOX_PERMISSION_LEVELS,
    SANDBOX_TENANCY_AGENT,
    SANDBOX_TENANCY_CONVERSATION,
    SandboxAllocation,
    SandboxCreateSpec,
    SandboxNetworkPolicy,
)
from astrabox.core.service.orchestrator.engine import transcript_mirror
from astrabox.seams.model import ResolvedModelAccess


logger = get_logger(__name__)


#: CPU ceiling of one sandbox box (see :func:`sandbox_create_resources`).
SANDBOX_CPU_LIMIT = 4


def sandbox_create_resources() -> tuple[dict[str, str], dict[str, str]]:
    """Return the platform's measured ``(limits, requests)`` box envelope.

    Limits are the runtime ceiling shared conversations consume; requests are
    only the Kubernetes scheduling reservation. Keeping the values separate
    preserves the last server-Pool recipe: five conversations can burst without
    CPU starvation or an OOM, while idle boxes do not reserve their ceilings.

    The Docker runtime drives the daemon of the host this server runs on, and
    Docker refuses a CPU limit above that host's CPU count ("range of CPUs is
    from 0.01 to 2.00"), so there the ceiling is at most the host's CPUs. A
    Kubernetes limit may exceed one node's capacity and keeps the recipe.
    """
    from astrabox.deploy.sandbox_server import RUNTIME_DOCKER, sandbox_runtime

    cpu = SANDBOX_CPU_LIMIT
    if sandbox_runtime() == RUNTIME_DOCKER:
        cpu = min(cpu, os.cpu_count() or 1)
    return (
        {"cpu": str(cpu), "memory": "4Gi"},
        {"cpu": "200m", "memory": "768Mi"},
    )


def _slot_model_substitution(
    slot_id: str, shared_credential: str
) -> tuple[ModelEgressCredentialSubstitution, ...]:
    return (
        ModelEgressCredentialSubstitution(
            name=workload_credential_name(slot_id),
            secret_value=shared_credential,
            placeholder=workload_model_placeholder(slot_id),
        ),
    )


@dataclass(frozen=True)
class ModelCredentialRequest:
    """How one engine's model access is delivered to its box.

    Its own contract rather than part of the create, because the decision is
    needed without a create: re-attaching to a running box refreshes the
    sidecar's write from exactly these inputs.
    """

    #: The platform-resolved access value. The adapter decides how its engine
    #: consumes these neutral facts and declares only the HTTP paths the
    #: credential vault must bind.
    access: ResolvedModelAccess
    #: Gateway paths, relative to ``model_base_url``, the sidecar may inject
    #: the credential into — the engine's own wire, e.g. ``chat/completions``.
    request_paths: tuple[str, ...]
    #: The error this engine raises when its environment supplies no model
    #: access. Owned by the adapter: it is the adapter's error vocabulary.
    missing_code: str
    missing_message: str
    missing_status: int = 409
    header: str = "Authorization"
    request_methods: tuple[str, ...] = ("POST",)


def model_credential_plan(
    credential: ModelCredentialRequest,
    *,
    substitutions: tuple[ModelEgressCredentialSubstitution, ...] = (),
) -> SandboxEgressCredentialPlan:
    """Build the provider-neutral model Vault plan for one engine wire."""

    model_api_key = str(credential.access.credential or "").strip()
    model_base_url = str(credential.access.base_url or "").strip()
    if not model_api_key or not model_base_url:
        raise APIError(
            code=credential.missing_code,
            message=credential.missing_message,
            status_code=credential.missing_status,
        )
    return SandboxEgressCredentialPlan(
        model=(
            ModelEgressCredential(
                name="astrabox-model-gateway",
                secret_value=model_api_key,
                credential_header=credential.header,
                base_url=model_base_url,
                request_methods=tuple(credential.request_methods),
                request_paths=tuple(
                    credential_request_path(model_base_url, path)
                    for path in credential.request_paths
                ),
                substitutions=substitutions,
            ),
        )
    )


@dataclass(frozen=True)
class EngineSandboxRequest:
    """What a box must be for one engine. Data, not a procedure."""

    #: The image's own entrypoint. Required, and required to be explicit: the
    #: provider's ``None`` default is the Claude agent image's boot script, so
    #: an omission is not "the image default" but "another engine's default".
    entrypoint: tuple[str, ...]
    credential: ModelCredentialRequest
    #: Everything else the image needs. The credential variable is composed in
    #: and must not appear here.
    env: Mapping[str, str] = field(default_factory=dict)
    #: The variable the engine's image reads its model credential from, when it
    #: reads one from the environment at all. Under the vault this carries a
    #: placeholder and the sidecar injects the real value. ``None`` for an
    #: engine that receives its credential another way — Hermes writes it into
    #: a profile file after the box exists — and the resolved value still comes
    #: back on the result for that engine to place.
    credential_env_var: str | None = None
    #: Request a platform-owned credential for the isolated runtime's control
    #: service. It is separate from model access and remains stable on claim.
    isolated_service_auth: bool = False
    #: Whether the platform plans this conversation's POSIX identity before
    #: create. ``False`` for an engine that provisions its own inside the box;
    #: the result then carries no plan rather than an unused one.
    plan_identity: bool = True
    #: The box's working directory. ``None`` takes the planned conversation
    #: workspace, which is what an engine wants unless its image needs the
    #: shared root left root-owned at boot.
    cwd: str | None = None
    #: The variable the image reads its working directory from, when it has
    #: one. The engine names the variable; the platform fills the value,
    #: because the platform is what decides the directory.
    cwd_env_var: str | None = None
    publish_ports: tuple[int, ...] = ()
    #: Hosts required by this engine's image preparation in addition to the
    #: model endpoint. The platform combines them with Environment policy; the
    #: adapter declares destinations but never applies policy itself.
    required_network_hosts: tuple[str, ...] = ()
    #: An in-box service port whose first accepted connection means the box is
    #: usable. The create blocks on it and kills a box that never serves, so an
    #: engine whose runtime starts at boot declares readiness here instead of
    #: discovering it on the first turn — a box that reached READY and cannot
    #: answer is the failure shape this whole contract exists to prevent.
    wait_for_inbox_service_port: int | None = None


@dataclass(frozen=True)
class ProvisionedEngineSandbox:
    """A created, tracked box and the facts an adapter needs to publish it."""

    sandbox: Any
    sandbox_id: str
    #: The planned conversation identity. Turn dispatch requires this on the
    #: session row, so the adapter must carry it onto the SessionRuntime; the
    #: startup worker persists it from there.
    runtime_identity: dict[str, Any] | None
    #: The directory the box actually starts in.
    cwd: str
    #: The credential the image will see: a placeholder under the vault.
    model_credential: str
    #: Direct MCP bindings installed at create. Their stable names let the
    #: refresh path identify the Session's destination and Vault scope.
    mcp_vault_write: SandboxEgressCredentialPlan | None = None
    #: Re-resolve and atomically reconcile MCP credentials before each new
    #: root input. The callback refuses a missing process-local Vault instead
    #: of creating an incomplete one without the model credential beside it.
    prepare_engine_input: Callable[[], Awaitable[None]] | None = None
    #: The conversation this box can actually rejoin. For an engine whose
    #: transcript the platform moves (``session_log``), this is the planned key
    #: only when the store had something to put back; when it had nothing the
    #: key is dropped and the engine starts a new conversation in this box. For
    #: every other engine it is the planned key unchanged — that transcript is
    #: not the platform's to judge.
    resume_session_key: str | None = None
    #: Opaque environment placeholders minted with the platform Vault write.
    #: Engine activation may pass these to its child, but never sees the secret
    #: values behind them.
    runtime_env: Mapping[str, str] = field(default_factory=dict)
    #: The platform manifest consumed when this runtime was claimed from a
    #: prepared unit.  Only engine-authored evidence is interpreted by the
    #: adapter; allocation and cleanup stay in the platform.
    prepared_manifest: dict[str, Any] | None = None
    #: A platform-resolved endpoint to an already prepared runner, when the
    #: unit includes one.
    runner_uri: str | None = None


def plan_conversation_identity(
    *, workspace_plan: Any, template: Any, session_id: str, user_id: str | None
) -> dict[str, Any] | None:
    """The workspace's own plan for this conversation's POSIX identity."""

    from astrabox.core.service.orchestrator.workspace import (
        workspace_from_subject_kind,
    )

    workspace = workspace_from_subject_kind(
        workspace_plan.subject_kind,
        user_id=user_id,
        agent_id=workspace_plan.agent_id,
        assistant_id=workspace_plan.assistant_id,
        engine_kind=workspace_plan.engine_kind,
    )
    planner = getattr(workspace, "plan_runtime_identity", None)
    if not callable(planner):
        return None
    planned = planner(template=template, session_id=session_id, user_id=user_id)
    return dict(planned) if isinstance(planned, dict) else None


def resolve_model_credential_delivery(
    *,
    template: Any,
    backend_adapter: Any,
    credential: ModelCredentialRequest,
    additional_vault_write: SandboxEgressCredentialPlan | None = None,
    required_hosts: tuple[str, ...] = (),
    mcp_hosts: tuple[str, ...] = (),
    slot_id: str | None = None,
    substitutions: tuple[ModelEgressCredentialSubstitution, ...] = (),
) -> tuple[str, SandboxNetworkPolicy, SandboxEgressCredentialPlan | None]:
    """Resolve independent networking and credential-delivery inputs.

    Networking is derived solely from the Environment plus platform-known
    runtime destinations. Protected delivery independently chooses whether the
    image receives the real credential or a placeholder backed by Vault. No
    binding host is copied into the Environment-derived policy; the provider
    later admits only the exact destinations named by attached bindings in its
    effective policy. No network mode enables or disables Vault.

    ``slot_id`` marks a box prepared under slot identity, before any Session
    exists. The box then receives the slot's OWN placeholder, the binding
    carries a substitution from it to a per-slot vault credential (written
    with the shared key, so a first call racing the claim's replace still
    authenticates), and the claim swaps that credential for a Session-scoped
    key without touching the box. Slot preparation is refused outright when
    the vault is off — that path would fix the real key inside an unclaimed
    box — and when the credential rides a non-Authorization header, because
    header substitution is the only mechanism that can re-point an already
    -running child's identity.
    """

    model_api_key = str(credential.access.credential or "").strip()
    model_base_url = str(credential.access.base_url or "").strip()
    if not model_api_key or not model_base_url:
        raise APIError(
            code=credential.missing_code,
            message=credential.missing_message,
            status_code=credential.missing_status,
        )
    network_policy = resolve_network_policy(
        template,
        required_hosts=(
            required_network_host(model_base_url, label="model endpoint"),
            *required_hosts,
        ),
        mcp_hosts=mcp_hosts,
    )
    vault_enabled = bool(
        getattr(load_astrabox_settings(), "sandbox_credential_vault_enabled", False)
    )
    engine_kind = str(getattr(template, "engine_kind", "") or "engine")
    target_slot = str(slot_id or "").strip() or None
    if not bool(getattr(backend_adapter, "supports_create_network_policy", False)):
        raise NotImplementedError(
            f"sandbox backend {backend_adapter.name!r} cannot enforce the "
            f"{engine_kind} Environment's networking contract"
        )
    if target_slot and not vault_enabled:
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message=(
                "engine slot preparation requires protected credential "
                "delivery so a model secret is not fixed in an unclaimed box"
            ),
            status_code=409,
        )
    if target_slot and str(credential.header or "").strip().lower() in (
        "x-api-key",
        "api-key",
    ):
        # An api-key binding rewrites its header box-wide and does not select
        # among placeholder substitutions. The claim could replace a named
        # credential, but the running child would still spend the shared one.
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message=(
                f"engine slot preparation cannot ride the {credential.header!r} "
                "header: per-slot identity needs Authorization-header "
                "substitution"
            ),
            status_code=409,
        )
    if not vault_enabled:
        return model_api_key, network_policy, None
    if not bool(getattr(backend_adapter, "supports_egress_credential_injection", False)):
        raise NotImplementedError(
            f"sandbox backend {backend_adapter.name!r} cannot keep the "
            f"{engine_kind} model credential outside the sandbox. Use a backend "
            "with egress credential injection, or turn "
            "ASTRABOX_SANDBOX_CREDENTIAL_VAULT off explicitly."
        )
    model_vault_write = model_credential_plan(
        credential,
        substitutions=(
            (*substitutions, *_slot_model_substitution(target_slot, model_api_key))
            if target_slot
            else substitutions
        ),
    )
    vault_write = merge_credential_plans(model_vault_write, additional_vault_write)
    if target_slot:
        return workload_model_placeholder(target_slot), network_policy, vault_write
    return EGRESS_HELD_PLACEHOLDER, network_policy, vault_write


async def resolve_session_environment_credentials(
    manager: Any,
    *,
    session_id: str,
    vault_enabled: bool,
    vault_write: SandboxEgressCredentialPlan | None,
    placeholder_context: str | None = None,
) -> tuple[SandboxEgressCredentialPlan | None, dict[str, str]]:
    """Compose Session outbound credentials and return only safe environment values."""

    credentials = await manager.resolve_session_egress_credentials(
        session_id,
        **(
            {"placeholder_context": placeholder_context}
            if placeholder_context is not None
            else {}
        ),
    )
    if not credentials:
        return vault_write, {}
    if not vault_enabled:
        raise APIError(
            code="SANDBOX_CREDENTIAL_VAULT_DISABLED",
            message=(
                "The managed Agent or Assistant uses an outbound credential, "
                "which requires protected delivery. Set "
                "ASTRABOX_SANDBOX_CREDENTIAL_VAULT=1, or remove that credential "
                "from the runtime binding."
            ),
            status_code=400,
        )
    from astrabox.seams.egress_credentials import HTTPBasicEgressCredentialSet

    environment = [item for item in credentials if not isinstance(item, HTTPBasicEgressCredentialSet)]
    combined = merge_credential_plans(
        vault_write,
        SandboxEgressCredentialPlan(
            environment=tuple(environment),
            http_basic=tuple(item for item in credentials if isinstance(item, HTTPBasicEgressCredentialSet)),
        ),
    )
    return combined, {str(item.secret_name): str(item.placeholder) for item in environment}


def environment_credential_contract(
    plan: SandboxEgressCredentialPlan | None,
) -> list[dict[str, Any]]:
    """Persist the non-secret shape a prepared process and Vault agreed on."""

    if plan is None:
        return []
    environment = [
        {
            "credential_id": str(item.credential_id),
            "secret_name": str(item.secret_name),
            "placeholder": str(item.placeholder),
            "networking": dict(item.networking),
            "injection_location": dict(item.injection_location),
            "allow_insecure_http": bool(item.allow_insecure_http),
            "allowed_requests": dict(item.allowed_requests),
        }
        for item in sorted(
            plan.environment,
            key=lambda credential: (
                str(credential.secret_name),
                str(credential.credential_id),
            ),
        )
    ]
    return [
        *environment,
        *(
            {
                "type": "http_basic", "credential_id": item.credential_id,
                "url": item.url, "username": item.username,
            }
            for group in plan.http_basic
            for item in sorted(group.credentials, key=lambda credential: credential.url)
        ),
    ]


async def resolve_prepared_environment_credentials(
    template: Any,
    *,
    slot_id: str,
    vault_enabled: bool,
    vault_write: SandboxEgressCredentialPlan | None,
) -> tuple[SandboxEgressCredentialPlan | None, dict[str, str]]:
    """Compose Agent-bound outbound credentials before a Session exists."""

    vault_ids = [
        str(item).strip()
        for item in (getattr(template, "credential_vault_ids", None) or [])
        if str(item or "").strip()
    ]
    if not vault_ids:
        return vault_write, {}
    if not vault_enabled:
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message=(
                "prepared runtimes with outbound credentials require "
                "protected credential delivery"
            ),
            status_code=409,
        )
    from astrabox.core.service.orchestrator.vault_service import VaultService
    from astrabox.seams.egress_credentials import HTTPBasicEgressCredentialSet, workload_placeholder_context

    credentials = await VaultService().resolve_egress_credentials(
        vault_ids,
        placeholder_context=workload_placeholder_context(slot_id),
    )
    environment = [item for item in credentials if not isinstance(item, HTTPBasicEgressCredentialSet)]
    combined = merge_credential_plans(
        vault_write,
        SandboxEgressCredentialPlan(
            environment=tuple(environment),
            http_basic=tuple(item for item in credentials if isinstance(item, HTTPBasicEgressCredentialSet)),
        ),
    )
    return combined, {str(item.secret_name): str(item.placeholder) for item in environment}


def _session_log_declaration(template: Any) -> Any:
    """The engine's session-log declaration, or None when it hands one over."""

    from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter

    kind = str(getattr(template, "engine_kind", "") or "").strip()
    if not kind:
        return None
    return getattr(get_engine_adapter(kind).capabilities, "session_log", None)


async def workspace_is_ready(box: Any, planned: tuple[tuple[str, str], ...], *, ref: Any) -> None:
    """Verify the storage medium and platform view for each planned mount.

    Called during workspace preparation before engine activation. The storage
    provider verifies its medium, then the platform router checks the mergerfs
    view. An empty plan needs no persistent storage. A nonempty plan requires
    an owning subject, and any failed check prevents use of that workspace.
    """

    if not planned:
        return
    if ref is None:
        raise RuntimeError("workspace mounts were planned without an owning subject")
    from astrabox.seams.storage import storage_provider

    provider = storage_provider()
    from astrabox.core.service.orchestrator.runtime.storage.mergerfs import workspace_router

    for box_path, _subpath in planned:
        await provider.prepare(ref, box=box, box_path=box_path)
        await workspace_router.prepare(ref, box=box, box_path=box_path)


async def resolve_sandbox_websocket_endpoint(sandbox: Any, port: int) -> str:
    """Resolve one box service endpoint into the URI an engine client uses.

    Endpoint publication and Secure Access signing are sandbox-platform work.
    Keeping this beside provisioning means an engine receives an address; it
    never asks a provider how the box is exposed.
    """

    endpoint = await sandbox.get_endpoint(int(port))
    if dict(getattr(endpoint, "headers", None) or {}):
        settings = load_astrabox_settings()
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(settings.sandbox_endpoint_url_ttl_seconds)
        )
        endpoint = await sandbox.get_signed_endpoint(int(port), expires_at=expires_at)
        if dict(getattr(endpoint, "headers", None) or {}):
            raise RuntimeError(
                "signed sandbox endpoint still requires routing headers the "
                f"engine transport cannot carry (sandbox={sandbox.sandbox_id})"
            )
    base = str(endpoint.endpoint).rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    raise RuntimeError(f"sandbox endpoint has no http(s) scheme: {base!r}")


async def prepare_platform_workspace(
    manager: Any,
    sandbox: Any,
    *,
    session_id: str,
    template: Any,
    workspace_plan: Any,
    user_id: str | None,
    runtime_identity: dict[str, Any] | None,
    cwd: str,
) -> tuple[dict[str, Any] | None, str]:
    """Materialize the platform workspace before an engine is activated.

    Sandbox creation supplies any configured workspace mounts. This step owns
    the product-level preparation that is the same for every engine: establish
    the conversation identity, install the Agent's declared capabilities, and
    materialize the default repository when the workspace plan asks for it.
    Engine adapters receive the resulting identity and directory; they do not
    choose or prepare storage.
    """

    from astrabox.core.service.orchestrator.workspace import (
        workspace_from_subject_kind,
    )

    workspace = workspace_from_subject_kind(
        str(getattr(workspace_plan, "subject_kind", "") or ""),
        user_id=user_id,
        agent_id=str(getattr(workspace_plan, "agent_id", "") or "") or None,
        assistant_id=(str(getattr(workspace_plan, "assistant_id", "") or "") or None),
        engine_kind=str(getattr(workspace_plan, "engine_kind", "") or ""),
    )
    await workspace.mount_and_provision(
        manager,
        sandbox,
        template=template,
        session_id=session_id,
        user_id=user_id,
        runtime_identity=runtime_identity,
    )
    provisioned_identity = getattr(workspace, "provisioned_runtime_identity", None)
    if isinstance(provisioned_identity, dict):
        runtime_identity = {
            **(runtime_identity or {}),
            **{key: value for key, value in provisioned_identity.items() if value is not None},
        }
    realized_cwd = str((runtime_identity or {}).get("workspace_dir") or cwd).strip()
    if getattr(workspace_plan, "materialize_default_repo", False):
        await manager.clone_default_repo(
            sandbox,
            template,
            str(getattr(workspace_plan, "default_repo_target_cwd", "") or "").strip(),
            str(getattr(workspace_plan, "conversation_session_id", "") or session_id),
            runtime_identity=runtime_identity,
        )
    return runtime_identity, realized_cwd


def workspace_ref_for_subject(
    *,
    subject_kind: str,
    agent_id: str | None,
    assistant_id: str | None,
    session_id: str | None = None,
) -> Any:
    """The workspace a create-time plan belongs to, from the subject's own facts.

    The create path is the only place this is asked, so it is built from the
    subject's own facts rather than from a session row: at create there is no
    row yet, and for a prepared slot there is no conversation at all — the mount
    is the Agent's root, which is what makes the box lendable to whichever
    conversation arrives.
    """

    from astrabox.seams.storage import WorkspaceRef

    kind = str(subject_kind or "").strip()
    if kind == "assistant_runtime":
        subject_id = str(assistant_id or "").strip()
        if not subject_id:
            return None
        return WorkspaceRef(subject_kind="assistant", subject_id=subject_id)
    if kind in {"deployment_conversation", "deployment_runtime"}:
        subject_id = str(agent_id or "").strip()
        if not subject_id:
            return None
        conversation = str(session_id or "").strip() if kind == "deployment_conversation" else ""
        return WorkspaceRef(
            subject_kind="agent",
            subject_id=subject_id,
            conversation_session_id=conversation or None,
        )
    return None


async def plan_workspace_mounts(
    *,
    subject_kind: str,
    session_id: str,
    agent_id: str | None,
    assistant_id: str | None,
    user_id: str | None,
    runtime_identity: dict[str, Any] | None,
    workspace_id: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Plan optional workspace mounts before the box starts.

    An empty volume setting requests a temporary workspace. Native session
    recovery uses the platform database independently of this mount plan.
    """
    kind = str(subject_kind or "").strip()
    if not kind:
        return ()
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.core.service.orchestrator.runtime.storage import (
        ensure_workspace_id,
        plan_subject_storage_mounts,
    )

    settings = load_astrabox_settings()
    if not str(getattr(settings, "sandbox_workspace_volume", "") or "").strip():
        return ()
    # Resolved once, here, because it is the same question for every subject and
    # every rung: what does the platform call this workspace. Minting it further
    # in would give one box a name the next box cannot find.
    resolved = workspace_id or await ensure_workspace_id(
        subject_kind=kind,
        session_id=session_id,
        agent_id=str(agent_id or "").strip() or None,
        assistant_id=str(assistant_id or "").strip() or None,
    )
    planned = plan_subject_storage_mounts(
        subject_kind=kind,
        workspace_id=resolved,
        # The runtime settings, which is where `nas_base_path` lives — the
        # process-wide `config.settings` object is a different one and does not
        # carry it.
        settings=settings,
        agent_id=str(agent_id or "").strip() or None,
        assistant_id=str(assistant_id or "").strip() or None,
        user_id=str(user_id or "").strip() or None,
        runtime_identity=runtime_identity,
    )
    return tuple((str(box_path), str(subpath)) for box_path, subpath in planned)


async def _assemble_provisioned_sandbox(
    sandbox: Any,
    *,
    manager: Any,
    backend_adapter: Any,
    sandbox_id: str,
    session_id: str,
    template: Any,
    workspace_plan: Any,
    runtime_identity: dict[str, Any] | None,
    cwd: str,
    credential: str,
    session_log: Any,
    mcp_vault_write: Any = None,
    vault_enabled: bool = False,
    runtime_env: Mapping[str, str] | None = None,
    prepared_manifest: dict[str, Any] | None = None,
    runner_uri: str | None = None,
) -> ProvisionedEngineSandbox:
    """The tenancy-agnostic tail: transcript restore, then the result.

    Both provisioning paths end here — a box of the conversation's own and
    a placement in its Agent's shared box — because what happens after a
    box exists does not depend on how it came to exist: an engine that
    rebuilds its conversation from a file needs that file back before
    anything asks it to rejoin, wherever its home is.
    """

    resume_key = str(getattr(workspace_plan, "resume_engine_session_key", "") or "").strip()
    honoured_resume_key: str | None = resume_key or None
    if session_log is not None and resume_key:
        # A new box for a conversation that already exists. Its engine rebuilds
        # that conversation from a file it expects to find, so the file goes
        # back before anything asks the engine to rejoin — after that, the
        # engine has already answered that it knows no such conversation.
        identity_home = str((runtime_identity or {}).get("home_dir") or "").strip()
        restored = await transcript_mirror.restore_mirrored_logs(
            sandbox,
            session_id,
            namespace=session_log.namespace,
            # Resolve the log root with this conversation's runtime identity so
            # shared-tenancy restore writes into the correct private home.
            root=session_log.rendered_root(home=identity_home),
            owner=str((runtime_identity or {}).get("linux_user") or "").strip() or None,
        )
        if not restored:
            # Nothing was ever mirrored for this conversation, so there is no
            # history to lose and no file for the engine to find. Naming the
            # planned key anyway would make the engine refuse the box outright
            # (Codex: `no rollout found for thread id`; pi exits with `No
            # session found matching`), which ends the session instead of
            # answering the next message. The box starts a fresh conversation
            # and the engine mints its own key; this line is the only place that
            # observes it, so it is logged rather than silent.
            logger.warning(
                "session %s carries resume key %s but the transcript store "
                "holds nothing to put back; starting a new %s conversation in "
                "this box",
                session_id,
                resume_key,
                str(getattr(template, "engine_kind", "") or "unknown"),
            )
            honoured_resume_key = None

    return ProvisionedEngineSandbox(
        sandbox=sandbox,
        sandbox_id=sandbox_id,
        runtime_identity=runtime_identity,
        cwd=cwd,
        model_credential=credential,
        mcp_vault_write=mcp_vault_write,
        prepare_engine_input=(
            mcp_credential_refresher(
                manager,
                session_id=session_id,
                template=template,
                backend_adapter=backend_adapter,
                sandbox=sandbox,
                initial_credential_plan=mcp_vault_write,
                vault_enabled=vault_enabled,
            )
            if mcp_vault_write is not None
            else None
        ),
        resume_session_key=honoured_resume_key,
        runtime_env=dict(runtime_env or {}),
        prepared_manifest=prepared_manifest,
        runner_uri=runner_uri,
    )


ENGINE_ENV_FILE_NAME = ".astrabox-engine-env"
SERVICE_CREDENTIAL_FILE_NAME = ".astrabox-service-credential"


def engine_service_credential(
    request: EngineSandboxRequest, runtime_identity: dict[str, Any] | None
) -> str | None:
    """Authenticate one physical runtime without binding it to its future claimant."""
    identity = dict(runtime_identity or {})
    if not request.isolated_service_auth or identity.get("sandbox_tenancy") != "agent":
        return None
    subject = [
        str(identity.get(key) or "").strip()
        for key in ("sandbox_id", "isolated_session_id")
    ]
    if not all(subject):
        raise RuntimeError(
            "isolated service authentication requires a complete runtime identity"
        )
    from astrabox.core.service.orchestrator.platform_secret import (
        derive_platform_key,
        platform_secret_root,
    )

    return derive_platform_key(
        platform_secret_root(),
        domain="astrabox-isolated-runtime-control",
        subject=json.dumps(subject, separators=(",", ":")),
    ).hex()


async def write_engine_env_file(
    sandbox: Any,
    *,
    request: "EngineSandboxRequest",
    credential: str,
    cwd: str,
    runtime_identity: dict[str, Any],
    owner_label: str,
    additional_env: Mapping[str, str] | None = None,
) -> None:
    """Write one conversation seat's engine environment into its home.

    The ONE composer for both writers — the live shared placement and a
    prewarmed slot's mint. A second hand-rolled copy is how the two paths
    drift: the slot's serve-conversation sources this file at pre-start, so a
    key present on one path and absent on the other fails only when pooling
    is switched on. The credential is whatever the caller resolved — a real
    value on the live path, the slot's vault placeholder on the mint path —
    and the file never needs rewriting on claim, because the egress sidecar
    substitutes placeholders per request.
    """

    engine_env: dict[str, str] = {
        **{str(key): str(value) for key, value in dict(request.env).items()},
        **{str(key): str(value) for key, value in dict(additional_env or {}).items()},
        **({request.credential_env_var: credential} if request.credential_env_var else {}),
        **({request.cwd_env_var: cwd} if request.cwd_env_var else {}),
    }
    home_dir = str(runtime_identity.get("home_dir") or "").strip()
    linux_user = str(runtime_identity.get("linux_user") or "").strip()
    if not home_dir or not linux_user:
        raise RuntimeError(
            f"{owner_label} placement carries no home/account to "
            "deliver the engine environment into"
        )
    service_credential = engine_service_credential(request, runtime_identity)
    if service_credential is not None:
        await sandbox.files.write_file(
            f"{home_dir.rstrip('/')}/{SERVICE_CREDENTIAL_FILE_NAME}",
            service_credential.encode("ascii"),
            mode=600,
            owner=linux_user,
            group=linux_user,
        )
    body = "".join(
        f"export {key}={shlex.quote(value)}\n" for key, value in sorted(engine_env.items())
    ).encode("utf-8")
    await sandbox.files.write_file(
        f"{home_dir.rstrip('/')}/{ENGINE_ENV_FILE_NAME}",
        body,
        mode=600,
        owner=linux_user,
        group=linux_user,
    )


def engine_create_spec(
    *,
    session_id: str,
    assignment_id: str,
    template: Any,
    request: EngineSandboxRequest,
    cwd: str,
    credential: str,
    mirror_env: Mapping[str, str],
    runtime_env: Mapping[str, str],
    network_policy: Any,
    permission_level: str,
    vault_write: Any,
    planned_mounts: tuple[tuple[str, str], ...],
    callback_url: str | None,
) -> SandboxCreateSpec:
    """What a box must BE, separate from where it comes from.

    The platform answers the second question — claim a prepared unit, place a
    conversation in its Agent's box, or create a box — and only the create needs
    this recipe. It is composed once for every engine and every placement so
    boxes of the same Environment cannot disagree on their boot environment or
    durable mounts.
    """

    resource_limits, resource_requests = sandbox_create_resources()
    return SandboxCreateSpec(
        session_id=session_id,
        assignment_id=assignment_id,
        resource_limits=resource_limits,
        resource_requests=resource_requests,
        image=resolve_runtime_template_name(template),
        entrypoint=tuple(request.entrypoint),
        cwd=cwd,
        env={
            **SANDBOX_SELF_DESCRIPTION,
            **dict(request.env),
            **dict(mirror_env),
            **dict(runtime_env),
            **({request.credential_env_var: credential} if request.credential_env_var else {}),
            **({request.cwd_env_var: cwd} if request.cwd_env_var else {}),
        },
        publish_ports=tuple(request.publish_ports),
        wait_for_inbox_service_port=request.wait_for_inbox_service_port,
        # Every box the platform hands to a conversation has commands run in it
        # before its engine starts — the mount check, the workspace
        # provisioning, the identity script. Proving the channel at create puts
        # that failure where the box is still just a box, instead of in the
        # first thing that tried to use it.
        requires_command_channel=True,
        death_callback_url=callback_url,
        network_policy=network_policy,
        permission_level=permission_level,
        vault_write=vault_write,
        workspace_mounts=planned_mounts,
    )


async def create_shared_agent_sandbox(
    backend_adapter: Any,
    *,
    assignment_id: str,
    template: Any,
    request: EngineSandboxRequest,
    runtime_identity: dict[str, Any],
    cwd: str,
    credential: str,
    runtime_env: Mapping[str, str],
    network_policy: Any,
    vault_write: Any,
    on_created: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[Any, str]:
    """Create and prove the Agent-owned base box for shared placement.

    This is the only create recipe for that box, whether a Session needs the
    first one now or the platform prepares it before any Session exists.  The
    provider receives one complete create request and never decides whether
    the box should be shared, prepared, or claimed.
    """

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    if not agent_id:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="shared Agent-box creation requires an Agent id",
            status_code=500,
        )
    if not bool(getattr(backend_adapter, "supports_correlated_create", False)):
        raise APIError(
            code="SANDBOX_CORRELATED_CREATE_UNSUPPORTED",
            message=(
                f"sandbox backend {backend_adapter.name!r} cannot recover an "
                "Agent-box create whose platform worker exits"
            ),
            status_code=501,
        )
    planned_mounts = await plan_workspace_mounts(
        subject_kind="deployment_runtime",
        session_id="",
        agent_id=agent_id,
        assistant_id=None,
        user_id=None,
        runtime_identity=runtime_identity,
    )
    workspace_ref = workspace_ref_for_subject(
        subject_kind="deployment_runtime",
        agent_id=agent_id,
        assistant_id=None,
    )
    from astrabox.core.service.orchestrator.agent.runtime_generation import (
        agent_runtime_owner_id,
    )

    candidate = await create_sandbox_with_storage(
        backend_adapter,
        engine_create_spec(
            session_id=agent_runtime_owner_id(agent_id),
            assignment_id=assignment_id,
            template=template,
            request=request,
            cwd=cwd,
            credential=credential,
            mirror_env={},
            runtime_env=runtime_env,
            network_policy=network_policy,
            permission_level=resolve_sandbox_permission_level(template),
            vault_write=vault_write,
            planned_mounts=planned_mounts,
            callback_url=None,
        )
    )
    candidate_id = extract_sandbox_id(candidate)
    try:
        if on_created is not None:
            await on_created(candidate_id)
        await workspace_is_ready(candidate, planned_mounts, ref=workspace_ref)
    except BaseException:
        destruction = await backend_adapter.confirm_destroyed(candidate_id)
        if not destruction.confirmed:
            logger.error(
                "shared Agent-box candidate cleanup was not confirmed: sandbox=%s detail=%s",
                candidate_id,
                destruction.detail,
            )
        raise
    return candidate, candidate_id


async def _provision_shared_conversation(
    manager: Any,
    backend_adapter: Any,
    *,
    session_id: str,
    assignment_id: str,
    template: Any,
    workspace_plan: Any,
    user_id: str | None,
    request: EngineSandboxRequest,
    runtime_identity: dict[str, Any],
    cwd: str,
    credential: str,
    runtime_env: Mapping[str, str],
    network_policy: Any,
    vault_write: Any,
    session_log: Any,
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
    recover_assignment: bool = False,
) -> tuple[Any, str, dict[str, Any], str]:
    """Place one conversation into its Agent's shared box and start its service.

    The Agent-shared tenancy is a platform capability — an application of
    OpenSandbox isolated sessions — so the placement recipe lives here once,
    for every engine this module provisions, instead of being copied into
    each adapter. The platform owns the box decision and isolated-session
    placement; the engine contributes only its launch line
    (``shared_conversation_service_launch``) and its process environment
    (:class:`EngineSandboxRequest`), which cannot ride box-create env here —
    the box may predate this Session — and so is written into the
    conversation's home for the launch script to source, the same channel
    the deferred mirror target already uses.

    Returns ``(sandbox_handle, sandbox_id, runtime_identity, service_uri)`` with the
    identity carrying the realized placement (box, uid/gid, isolated
    sessions); the caller's transcript-restore and result assembly are
    tenancy-agnostic and continue on top.
    """

    from astrabox.core.service.orchestrator.runtime.conversation_identity import (
        conversation_identity_from_plan,
    )
    from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
        SharedSandboxLease,
    )
    from astrabox.persistence.repository.agent_repository import AgentRepository

    # A claimed slot's account and paths were named before its Session existed.
    # Retain that complete filesystem identity on re-borrow: combining its UID
    # with a newly rendered username collides with the account in a shared box.
    persisted = await SessionRepository().get_session(session_id)
    persisted_identity = (
        (persisted or {}).get("runtime_identity") if isinstance(persisted, dict) else None
    )
    if isinstance(persisted_identity, dict):
        for key in (
            "linux_user", "home_dir", "workspace_dir", "workspace_source_dir",
            "file_root_dir", "file_root_source_dir", "config_dir", "cache_dir",
            "temp_dir", "uid", "gid",
        ):
            value = persisted_identity.get(key)
            if value is not None:
                runtime_identity[key] = value
        from astrabox.core.service.orchestrator.runtime.runtime_profile import (
            assert_identity_boundary_complete,
            plan_capabilities,
        )

        capability_plan = plan_capabilities(template, runtime_identity)
        assert_identity_boundary_complete(runtime_identity, capability_plan)
        runtime_identity["capability_plan"] = capability_plan
    conversation = conversation_identity_from_plan(runtime_identity, workspace_plan)
    if conversation is None:
        raise RuntimeError(
            f"session {session_id!r} resolved the agent tenancy but its "
            "identity plan carries no shareable conversation (agent/home/"
            "workspace incomplete); refusing a placement that would guess"
        )
    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    if not agent_id:
        raise RuntimeError(f"session {session_id!r} resolved Agent tenancy without an Agent id")
    lease = SharedSandboxLease(
        agent_repo=AgentRepository(),
        provider=backend_adapter,
    )
    placed = None
    candidate_allocation: SandboxAllocation | None = None
    if not recover_assignment:
        placed = await lease.place_in_agent_box(
            agent_id=agent_id,
            home_dir=str(getattr(conversation, "home_dir", "") or ""),
            workspace_dir=str(getattr(conversation, "workspace_dir", "") or ""),
            workspace_source_dir=str(getattr(conversation, "workspace_source_dir", "") or ""),
            expected_runtime_generation=str(
                getattr(template, "runtime_generation", "") or ""
            ).strip()
            or None,
            expected_sandbox_generation=str(
                getattr(template, "sandbox_generation", "") or ""
            ).strip()
            or None,
            session_id=session_id,
            requested_uid=(
                int(runtime_identity["uid"]) if runtime_identity.get("uid") is not None else None
            ),
        )
    candidate: Any | None = None
    candidate_id = ""
    create_seam_carried_vault = False
    if placed is None:
        # There is no resident Agent box.  The platform creates one complete
        # candidate, including the Agent-owned durable root, then atomically
        # offers it to the Agent row. The candidate comes from the provider's
        # maintained client pool when one is idle; only an actually empty pool
        # reaches the same cold-create recipe. A correlated create recovery has
        # priority over both, so a replay never takes a second box first.
        if not recover_assignment:
            from astrabox.core.service.orchestrator.agent.client_pool import (
                acquire_agent_client_pool,
            )

            pool_claim = await acquire_agent_client_pool(
                template,
                runtime_manager=manager,
                session_id=session_id,
                assignment_id=assignment_id,
            )
            if pool_claim is not None:
                candidate = pool_claim.sandbox
                candidate_id = pool_claim.sandbox_id
                candidate_allocation = SandboxAllocation(
                    sandbox_id=candidate_id,
                    sandbox_backend=str(backend_adapter.name),
                    scope="sandbox",
                )
        if candidate is None:
            candidate, candidate_id = await create_shared_agent_sandbox(
                backend_adapter,
                assignment_id=assignment_id,
                template=template,
                request=request,
                runtime_identity=runtime_identity,
                cwd=cwd,
                credential=credential,
                runtime_env=runtime_env,
                network_policy=network_policy,
                vault_write=vault_write,
            )
            # ``vault_write`` is part of SandboxCreateSpec's required result,
            # including when a provider converges an exact-assignment replay
            # onto its existing resource. The create seam has therefore
            # established this candidate's Vault before returning it.
            create_seam_carried_vault = True
            recorded = (persisted or {}).get("startup_allocation")
            if isinstance(recorded, dict) and recorded.get("sandbox_id") == candidate_id:
                candidate_allocation = SandboxAllocation.from_record(recorded)
        try:
            placed = await lease.place_in_agent_box(
                agent_id=agent_id,
                home_dir=str(getattr(conversation, "home_dir", "") or ""),
                workspace_dir=str(getattr(conversation, "workspace_dir", "") or ""),
                workspace_source_dir=str(getattr(conversation, "workspace_source_dir", "") or ""),
                candidate=candidate_id,
                expected_runtime_generation=str(
                    getattr(template, "runtime_generation", "") or ""
                ).strip()
                or None,
                expected_sandbox_generation=str(
                    getattr(template, "sandbox_generation", "") or ""
                ).strip()
                or None,
                session_id=session_id,
                requested_uid=(
                    int(runtime_identity["uid"])
                    if runtime_identity.get("uid") is not None
                    else None
                ),
            )
            if placed is None:
                raise APIError(
                    code="SANDBOX_ISOLATION_UNSUPPORTED",
                    message=(
                        f"sandbox {candidate_id!r} cannot host the Agent-shared "
                        "conversation requested by this Environment"
                    ),
                    status_code=409,
                )
        except BaseException:
            agent_row = await AgentRepository().get_agent(agent_id)
            published = str((agent_row or {}).get("sandbox_id") or "").strip()
            if published != candidate_id:
                destruction = await backend_adapter.confirm_destroyed(candidate_id)
                if not destruction.confirmed:
                    logger.error(
                        "unadopted Agent-box candidate survived failed placement: "
                        "sandbox=%s detail=%s",
                        candidate_id,
                        destruction.detail,
                    )
            raise

    sandbox_id = str(placed.sandbox_id)
    isolated_session_ids = tuple(
        str(value or "").strip()
        for value in (
            placed.isolated_session_id,
            placed.terminal_isolated_session_id,
        )
        if str(value or "").strip()
    )
    allocation = SandboxAllocation(
        sandbox_id=sandbox_id,
        sandbox_backend=str(backend_adapter.name),
        scope="isolated_sessions",
        isolated_session_ids=isolated_session_ids,
    )
    unused_candidate_id = (
        candidate_id
        if candidate is not None and candidate_id and candidate_id != sandbox_id
        else ""
    )
    try:
        # Placement has already opened both isolated sessions. Name that exact
        # scope before any later provider or workspace operation can fail. The
        # manager remembers the allocation before its durable write, so even a
        # failed record is released by startup rollback without guessing at the
        # longer-lived Agent box.
        if candidate_allocation is not None:
            await manager.record_startup_allocation(
                session_id, allocation, replaces=candidate_allocation,
            )
        else:
            await manager.record_startup_allocation(session_id, allocation)
    finally:
        if unused_candidate_id and candidate_allocation is None:
            # Another concurrent creator won the Agent-row CAS. Its box is the
            # one this Session joined; this unpublished candidate has no owner.
            # Cleanup is independent of connect and of allocation persistence:
            # neither failure may strand the candidate.
            destruction = await backend_adapter.confirm_destroyed(unused_candidate_id)
            if not destruction.confirmed:
                raise APIError(
                    code="SANDBOX_CLEANUP_UNCONFIRMED",
                    message=(
                        f"unused Agent-box candidate {unused_candidate_id!r} could not "
                        f"be destroyed: {destruction.detail}"
                    ),
                    status_code=502,
                    data={"leaked_sandbox_id": unused_candidate_id},
                )

    if candidate is not None and candidate_id == sandbox_id:
        sandbox = candidate
    else:
        sandbox = await backend_adapter.connect(sandbox_id)
    if vault_write is not None and not (
        create_seam_carried_vault
        and candidate is sandbox
        and candidate_id == sandbox_id
    ):
        await backend_adapter.apply_credential_vault(
            sandbox,
            vault_write=vault_write,
            create_if_missing=False,
        )
    runtime_identity = {
        **runtime_identity,
        "sandbox_id": sandbox_id,
        "uid": int(placed.uid),
        "gid": int(placed.gid),
        "isolated_session_id": str(placed.isolated_session_id),
        "terminal_isolated_session_id": str(placed.terminal_isolated_session_id),
    }
    if progress_callback is not None:
        with contextlib.suppress(Exception):
            await progress_callback("mounting_nas")
    runtime_identity, cwd = await prepare_platform_workspace(
        manager,
        sandbox,
        session_id=session_id,
        template=template,
        workspace_plan=workspace_plan,
        user_id=user_id,
        runtime_identity=runtime_identity,
        cwd=cwd,
    )

    # The engine's process environment, sourced by its launch script. It
    # cannot ride the box (which may predate this Session) and there is no
    # host-config channel on this path, so the home — created by the
    # placement above, owned by the conversation account — is the channel.
    await write_engine_env_file(
        sandbox,
        request=request,
        credential=credential,
        cwd=cwd,
        runtime_identity=runtime_identity,
        owner_label=f"session {session_id!r}",
        additional_env=runtime_env,
    )
    home_dir = str(runtime_identity.get("home_dir") or "").strip()
    linux_user = str(runtime_identity.get("linux_user") or "").strip()

    if session_log is not None:
        # A mirror-file engine's per-conversation relay starts with the
        # launch below and reads its destination from the deferred target
        # file in this home (the launch script names it); written first so
        # the relay's fail-loud grace clock never starts in a healthy
        # placement.
        await transcript_mirror.bind_mirror_target(
            sandbox,
            manager,
            session_id,
            cwd=cwd,
            target_file=f"{home_dir.rstrip('/')}/.astrabox-mirror-target",
            owner=linux_user,
        )

    # Starts (or adopts) this conversation's own service through the engine's
    # launch declaration and waits for its port to listen. Endpoint resolution
    # is platform work too: the engine receives a ready address, not a provider
    # handle it must interpret.
    from astrabox.core.service.orchestrator.engine.registry import (
        get_engine_adapter,
    )
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        runner_port_for_uid,
    )

    engine_kind = str(getattr(template, "engine_kind", "") or "").strip()
    adapter = get_engine_adapter(engine_kind)
    runner_port = int(
        await lease.start_runner(
            placed,
            launch=adapter.shared_conversation_service_launch(
                home=placed.home_dir,
                workspace=placed.workspace_dir,
                port=runner_port_for_uid(placed.uid),
            ),
            engine_label=engine_kind,
        )
    )
    runner_uri = await resolve_sandbox_websocket_endpoint(sandbox, runner_port)
    return sandbox, sandbox_id, runtime_identity, runner_uri


async def require_engine_sandbox_permission(
    sandbox: Any,
    *,
    sandbox_id: str,
    template: Any,
) -> None:
    """Require a connected box to attest the Environment's current grant."""

    from astrabox.seams.sandbox import sandbox_for_sandbox

    permission_level = resolve_sandbox_permission_level(template)
    if permission_level == SANDBOX_PERMISSION_LEVEL_DEFAULT:
        return
    if permission_level not in SANDBOX_PERMISSION_LEVELS:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
            message=(
                f"sandbox_permission_level {permission_level!r} is not one of "
                f"{', '.join(repr(item) for item in SANDBOX_PERMISSION_LEVELS)}"
            ),
            status_code=409,
        )
    provider = sandbox_for_sandbox(sandbox)
    if provider is None:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
            message=(
                f"sandbox {sandbox_id!r} cannot be mapped to a provider that "
                f"can attest sandbox_permission_level {permission_level!r}"
            ),
            status_code=409,
        )
    supported = tuple(
        getattr(
            provider,
            "supported_permission_levels",
            (SANDBOX_PERMISSION_LEVEL_DEFAULT,),
        )
    )
    if permission_level not in supported:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
            message=(
                f"sandbox backend {provider.name!r} cannot attest "
                f"sandbox_permission_level {permission_level!r}"
            ),
            status_code=409,
        )
    if permission_level == SANDBOX_PERMISSION_LEVEL_ADVANCED:
        reported = await provider.read_isolation_capability(sandbox_id)
        if reported.available:
            return
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(
                f"sandbox {sandbox_id!r} does not provide the requested "
                f"'advanced' permission level: "
                f"{reported.detail or 'isolation capability unavailable'}"
            ),
            status_code=409,
            data={
                "sandbox_id": sandbox_id,
                "sandbox_permission_level": permission_level,
            },
        )
    # No registered in-tree provider reaches this branch. Keep the refusal
    # explicit: supporting a value and proving it are separate contracts.
    raise APIError(
        code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
        message=(
            f"sandbox backend {provider.name!r} does not expose an attestation "
            f"for sandbox_permission_level {permission_level!r}"
        ),
        status_code=409,
    )


async def connect_engine_sandbox(
    manager: Any,
    *,
    sandbox_id: str,
    template: Any,
) -> Any:
    """Reattach an engine only after its running box proves the requested grant.

    A persisted sandbox id proves identity, not posture. The Environment may
    have changed since this box was created, and a provider pool recipe may
    have drifted independently. Reattachment therefore asks the running box
    the same question the create path asks before an engine can use it.
    """

    sandbox = await manager.connect_sandbox_only(sandbox_id)
    try:
        await require_engine_sandbox_permission(
            sandbox,
            sandbox_id=sandbox_id,
            template=template,
        )
        return sandbox
    except BaseException:
        with contextlib.suppress(Exception):
            await sandbox.close()
        raise


async def prepare_attached_mcp_vault(
    manager: Any,
    *,
    session_id: str,
    template: Any,
    sandbox: Any,
) -> Callable[[], Awaitable[None]] | None:
    """Refresh direct MCP bindings on a box that outlived this host.

    This is deliberately patch-only. A missing process-local Vault would also
    be missing the model credential, and creating one from the MCP subset here
    would turn a precise attach failure into an opaque model 401.
    """

    vault_enabled = bool(
        getattr(load_astrabox_settings(), "sandbox_credential_vault_enabled", False)
    )
    mcp_vault_write = await resolve_mcp_credential_plan(
        manager,
        session_id=session_id,
        template=template,
        vault_enabled=vault_enabled,
    )
    if mcp_vault_write is None:
        return None

    from astrabox.seams.sandbox import sandbox_for_sandbox

    backend_adapter = sandbox_for_sandbox(sandbox)
    if backend_adapter is None:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                f"sandbox for Session {session_id!r} has no registered backend "
                "for MCP credential refresh"
            ),
            status_code=500,
        )
    if vault_enabled and not mcp_vault_write.is_empty:
        await backend_adapter.apply_credential_vault(
            sandbox,
            vault_write=mcp_vault_write,
            create_if_missing=False,
        )
    return mcp_credential_refresher(
        manager,
        session_id=session_id,
        template=template,
        backend_adapter=backend_adapter,
        sandbox=sandbox,
        initial_credential_plan=mcp_vault_write,
        vault_enabled=vault_enabled,
    )


async def claim_prepared_engine_sandbox(
    manager: Any,
    *,
    session_id: str,
    assignment_id: str,
    template: Any,
    workspace_plan: Any,
    user_id: str | None,
    request: EngineSandboxRequest,
    backend_adapter: Any,
    credential: str,
    mcp_vault_write: SandboxEgressCredentialPlan | None,
    vault_enabled: bool,
    session_log: Any,
    prepared_claim: dict[str, Any] | None = None,
    claimed_sandbox: Any = None,
) -> ProvisionedEngineSandbox | None:
    """Claim one platform-prepared unit, or report that none is available.

    This function owns the entire hand-off before engine activation: the Agent
    manifest CAS, provider identity adoption, durable cleanup record, credential
    re-point, workspace proof, transcript target, and prepared-runner endpoint.
    An adapter receives only the resulting box and its own opaque evidence.
    """

    if (
        str(getattr(workspace_plan, "resume_engine_session_key", "") or "").strip()
        and str(load_astrabox_settings().sandbox_workspace_volume or "").strip()
        and prepared_claim is None
    ):
        # Only a dedicated entry can be rebound before delivery. A shared
        # box's root also serves active siblings and cannot change as a whole.
        return None
    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    if not agent_id or not generation or not bool(getattr(template, "prewarm_enabled", False)):
        return None

    from astrabox.core.service.orchestrator.agent.prepared_boxes import (
        PLACEMENT_CONVERSATION_BOX,
        adopt_claimed_box,
    )
    from astrabox.core.service.orchestrator.agent.prepared_slots import (
        claim_prepared_slot,
        repoint_slot_gateway_credential,
        schedule_prepared_runtime_refill,
    )

    claimed = prepared_claim
    if claimed is None:
        claimed = await claim_prepared_slot(
            agent_id=agent_id,
            session_id=session_id,
            expected_runtime_generation=generation,
        )
    if claimed is None:
        # This Session paid the cold-start cost because eligible prepared
        # capacity was empty. Refill immediately so the next Session can
        # claim; the level-triggered scheduler coalesces concurrent misses.
        schedule_prepared_runtime_refill(template, manager)
        return None
    expected_engine = str(getattr(template, "engine_kind", "") or "").strip()
    actual_engine = str(claimed.get("engine_kind") or "").strip()
    if actual_engine != expected_engine:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                f"prepared runtime belongs to engine {actual_engine!r}, not "
                f"the selected engine {expected_engine!r}"
            ),
            status_code=409,
        )
    slot_id = str(claimed.get("slot_id") or "").strip()
    if not slot_id:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="claimed prepared runtime carries no workload id",
            status_code=500,
        )
    environment_plan, claimed_runtime_env = (
        await resolve_session_environment_credentials(
            manager,
            session_id=session_id,
            vault_enabled=vault_enabled,
            vault_write=None,
            placeholder_context=workload_placeholder_context(slot_id),
        )
    )
    expected_environment_contract = claimed.get("environment_credential_contract")
    if not isinstance(expected_environment_contract, list):
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                "claimed prepared runtime carries no environment-credential "
                "contract"
            ),
            status_code=409,
        )
    current_environment_contract = environment_credential_contract(environment_plan)
    prepared_runtime_env = claimed.get("runtime_env")
    if (
        not isinstance(prepared_runtime_env, dict)
        or prepared_runtime_env != claimed_runtime_env
        or expected_environment_contract != current_environment_contract
    ):
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                "the Session's protected environment credentials no longer "
                "match the process and network policy frozen into this "
                "prepared runtime"
            ),
            status_code=409,
        )

    placement = str(claimed.get("placement") or "")
    identity = dict(claimed.get("runtime_identity") or {})
    identity["session_id"] = session_id
    sandbox_id = str(claimed.get("sandbox_id") or "").strip()
    identity["sandbox_id"] = sandbox_id
    if bool(claimed.get("gateway_substitution")):
        # The process in a prepared unit keeps spending the placeholder it was
        # born with. Persist which platform credential scope backs that
        # placeholder so a later host can reconstruct the same Vault binding
        # without the now-consumed prepared manifest.
        identity["credential_slot_id"] = str(claimed.get("slot_id") or "")
    if placement == PLACEMENT_CONVERSATION_BOX:
        sandbox = claimed_sandbox
        if sandbox is None:
            sandbox = await adopt_claimed_box(
                template,
                claimed,
                session_id=session_id,
                assignment_id=assignment_id,
            )
        allocation = SandboxAllocation(
            sandbox_id=sandbox_id,
            sandbox_backend=str(backend_adapter.name),
            scope="sandbox",
        )
    elif placement == "shared_slot":
        if not str(identity.get("home_dir") or "").strip() or not str(
            identity.get("linux_user") or ""
        ).strip():
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message="claimed shared slot carries no home/account identity",
                status_code=500,
            )
        sandbox = await backend_adapter.connect(sandbox_id)
        isolated_session_ids = tuple(
            value
            for value in (
                str(claimed.get("isolated_session_id") or "").strip(),
                str(claimed.get("terminal_isolated_session_id") or "").strip(),
            )
            if value
        )
        allocation = SandboxAllocation(
            sandbox_id=sandbox_id,
            sandbox_backend=str(backend_adapter.name),
            scope="isolated_sessions",
            isolated_session_ids=isolated_session_ids,
        )
    else:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=f"prepared runtime names unknown placement {placement!r}",
            status_code=500,
        )
    await require_engine_sandbox_permission(
        sandbox,
        sandbox_id=sandbox_id,
        template=template,
    )
    await manager.record_startup_allocation(session_id, allocation)

    if prepared_claim is not None and bool(claimed.get("gateway_substitution")):
        from astrabox.core.service.orchestrator.agent.prepared_slots import record_gateway_entry
        from astrabox.persistence.repository.agent_repository import AgentRepository

        await record_gateway_entry(
            AgentRepository(), agent_id, slot_id=slot_id, session_id=session_id,
        )

    supplemental = merge_credential_plans(mcp_vault_write, environment_plan)
    if supplemental is not None and not supplemental.is_empty:
        await backend_adapter.apply_credential_vault(
            sandbox,
            vault_write=supplemental,
            create_if_missing=False,
        )
    await repoint_slot_gateway_credential(
        sandbox,
        backend_provider=backend_adapter,
        credential_request=request.credential,
        template=template,
        claimed=claimed,
        session_id=session_id,
        user_id=user_id,
    )

    cwd = (
        str(identity.get("workspace_dir") or "").strip()
        or str(claimed.get("cwd") or "").strip()
        or str(getattr(workspace_plan, "cwd", "") or "").strip()
    )
    workspace_kind = (
        "deployment_runtime"
        if placement == "shared_slot"
        else str(getattr(workspace_plan, "subject_kind", "") or "")
    )
    planned_mounts = await plan_workspace_mounts(
        subject_kind=workspace_kind,
        session_id=session_id,
        agent_id=agent_id,
        assistant_id=str(getattr(workspace_plan, "assistant_id", "") or ""),
        user_id=str(getattr(workspace_plan, "user_id", "") or user_id or ""),
        runtime_identity=identity,
        workspace_id=None,
    )
    if placement == PLACEMENT_CONVERSATION_BOX:
        await bind_prepared_workspace(
            backend=backend_adapter,
            sandbox_id=sandbox_id,
            session_id=session_id,
            mounts=planned_mounts,
        )
    await workspace_is_ready(
        sandbox,
        planned_mounts,
        ref=workspace_ref_for_subject(
            subject_kind=workspace_kind,
            agent_id=agent_id,
            assistant_id=str(getattr(workspace_plan, "assistant_id", "") or ""),
            session_id=session_id,
        ),
    )
    if session_log is not None:
        home_dir = str(identity.get("home_dir") or "").strip().rstrip("/")
        await transcript_mirror.bind_mirror_target(
            sandbox,
            manager,
            session_id,
            cwd=cwd,
            **(
                {
                    "target_file": f"{home_dir}/.astrabox-mirror-target",
                    "owner": str(identity.get("linux_user") or "").strip() or None,
                }
                if placement == "shared_slot"
                else {}
            ),
        )
    runner_uri = None
    runner_port = int(claimed.get("runner_port") or 0)
    if runner_port:
        runner_uri = await resolve_sandbox_websocket_endpoint(sandbox, runner_port)
    return await _assemble_provisioned_sandbox(
        sandbox,
        manager=manager,
        backend_adapter=backend_adapter,
        sandbox_id=sandbox_id,
        session_id=session_id,
        template=template,
        workspace_plan=workspace_plan,
        runtime_identity=identity,
        cwd=cwd,
        credential=(
            workload_model_placeholder(str(claimed.get("slot_id") or ""))
            if bool(claimed.get("gateway_substitution"))
            else credential
        ),
        session_log=session_log,
        mcp_vault_write=mcp_vault_write,
        vault_enabled=vault_enabled,
        prepared_manifest=claimed,
        runtime_env=claimed_runtime_env,
        runner_uri=runner_uri,
    )


async def _discard_failed_prepared_claim(
    manager: Any,
    *,
    session_id: str,
    template: Any,
    reason: str,
) -> None:
    """Dispose the exact durable claim left by a failed pre-activation handoff."""

    from astrabox.core.service.orchestrator.agent.prepared_boxes import (
        PLACEMENT_CONVERSATION_BOX,
        discard_prepared_box,
    )
    from astrabox.core.service.orchestrator.agent.prepared_slots import (
        PREPARED_SLOT_FIELD,
        discard_prepared_slot,
        release_claimed_slot_allocation,
        schedule_prepared_runtime_refill,
    )
    from astrabox.persistence.repository.agent_repository import AgentRepository

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    if not agent_id:
        return
    row = await AgentRepository().get_agent(agent_id)
    manifest = (row or {}).get(PREPARED_SLOT_FIELD)
    if (
        not isinstance(manifest, dict)
        or str(manifest.get("state") or "") != "claimed"
        or str(manifest.get("claimed_session_id") or "").strip() != session_id
    ):
        return
    placement = str(manifest.get("placement") or "").strip()
    if placement == PLACEMENT_CONVERSATION_BOX:
        await discard_prepared_box(agent_id, manifest, reason=reason)
    elif placement == "shared_slot":
        await discard_prepared_slot(agent_id, manifest, reason=reason)
    else:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                "failed prepared-runtime claim has no platform-owned placement "
                "kind and cannot be cleaned up safely"
            ),
            status_code=500,
        )
    await release_claimed_slot_allocation(session_id, manifest)
    schedule_prepared_runtime_refill(template, manager)


@dataclass(frozen=True)
class _ConversationPoolClaim:
    sandbox: Any
    sandbox_id: str
    workload_id: str
    workspace_id: str


async def _claim_conversation_pool_box(
    manager: Any,
    *,
    session_id: str,
    assignment_id: str,
    template: Any,
    backend_adapter: Any,
    workspace_plan: Any,
    recovered_assignment: Any,
) -> _ConversationPoolClaim | None:
    """Recover or atomically acquire the Session's official-pool box."""

    from astrabox.core.service.orchestrator.agent.client_pool import (
        acquire_agent_client_pool,
        conversation_pool_receipt,
    )

    session = await SessionRepository().get_session(session_id)
    existing_workspace_id = str((session or {}).get(WORKSPACE_ID_FIELD) or "").strip()

    if recovered_assignment is not None:
        receipt = conversation_pool_receipt(recovered_assignment.metadata)
        if receipt is None:
            return None
        ownership = await backend_adapter.claim_of(
            recovered_assignment.sandbox_id,
            expected_session_id=session_id,
        )
        if not ownership.may_destroy:
            raise APIError(
                code="SANDBOX_ASSIGNMENT_CONFLICT",
                message=(
                    f"recovered client-pool sandbox {recovered_assignment.sandbox_id!r} "
                    f"is not owned by Session {session_id!r}: {ownership.detail}"
                ),
                status_code=409,
            )
        workload_id, workspace_id = receipt
        return _ConversationPoolClaim(
            sandbox=await backend_adapter.connect(recovered_assignment.sandbox_id),
            sandbox_id=recovered_assignment.sandbox_id,
            workload_id=workload_id,
            workspace_id=existing_workspace_id or workspace_id,
        )

    acquired = await acquire_agent_client_pool(
        template,
        runtime_manager=manager,
        session_id=session_id,
        assignment_id=assignment_id,
    )
    if acquired is None:
        return None
    workload_id = str(acquired.workload_id or "").strip()
    workspace_id = str(acquired.workspace_id or "").strip()
    if not workload_id or not workspace_id:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                f"client-pool sandbox {acquired.sandbox_id!r} returned no "
                "conversation claim receipt"
            ),
            status_code=500,
        )
    return _ConversationPoolClaim(
        sandbox=acquired.sandbox,
        sandbox_id=acquired.sandbox_id,
        workload_id=workload_id,
        # Borrowing replacement compute must not replace the Session's file
        # identity. The storage entry is bound before engine activation.
        workspace_id=existing_workspace_id or workspace_id,
    )


async def provision_engine_sandbox(
    manager: Any,
    *,
    session_id: str,
    assignment_id: str,
    template: Any,
    workspace_plan: Any,
    user_id: str | None,
    callback_url: str | None,
    request: EngineSandboxRequest,
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> ProvisionedEngineSandbox:
    """Create and track one engine's box from its declaration.

    The order is the contract: the platform decides where, the workspace plans
    who, the vault decides what the box may see, and only then asks the backend
    to create the declared resource. The allocation is tracked before anything
    downstream can fail and orphan it.
    """

    from astrabox.seams.sandbox import sandbox_for_name

    composed = {request.credential_env_var, request.cwd_env_var} - {None}
    conflicting = sorted(composed & set(request.env))
    if conflicting:
        raise RuntimeError(
            f"engine env must not set {conflicting}: the platform composes "
            "those, so a value declared here would either disagree with the "
            "sidecar's real credential or name a directory the box is not in"
        )

    # Shared placement must remain compatible even when proactive preparation
    # is disabled. Publish the platform generation on the authoritative Session
    # path too, closing the race between an Agent edit and its background
    # reconciliation before any prepared unit or resident box is considered.
    if str(getattr(template, "agent_id", "") or "").strip():
        from astrabox.core.service.orchestrator.agent.runtime_generation import (
            reconcile_runtime_generation,
        )

        await reconcile_runtime_generation(template, runtime_manager=manager)

    persisted_backend = await manager.resolve_runtime_sandbox_backend(
        session_id, workspace_plan=workspace_plan
    )
    backend_adapter = sandbox_for_name(persisted_backend)
    if not bool(getattr(backend_adapter, "supports_correlated_create", False)):
        raise APIError(
            code="SANDBOX_CORRELATED_CREATE_UNSUPPORTED",
            message=(
                f"sandbox backend {backend_adapter.name!r} cannot recover a "
                "create whose worker exits before publication"
            ),
            status_code=501,
        )

    # Recovery precedes every placement or pool claim. A worker may have
    # created this command's exact box and died before publishing it; taking a
    # different prepared unit first would strand that box and let one durable
    # command own two resources. The later create path reconnects and re-proves
    # the correlated box through the provider's normal create contract. Its
    # runtime-owner metadata is provider wire data, so ownership comparison also
    # stays in that create contract rather than being reimplemented here.
    recovered_assignment = await backend_adapter.find_sandbox_by_assignment(assignment_id)

    runtime_identity = (
        plan_conversation_identity(
            workspace_plan=workspace_plan,
            template=template,
            session_id=session_id,
            user_id=user_id,
        )
        if request.plan_identity
        else None
    )
    cwd = str(request.cwd or "").strip()
    if not cwd and runtime_identity:
        cwd = str(runtime_identity.get("workspace_dir") or "").strip()
    if not cwd:
        cwd = str(getattr(workspace_plan, "cwd", "") or "").strip()
    if not cwd:
        raise RuntimeError(f"engine sandbox for session {session_id!r} has no working directory")

    tenancy = resolve_sandbox_tenancy(template)
    conversation_pool_claim = (
        await _claim_conversation_pool_box(
            manager,
            session_id=session_id,
            assignment_id=assignment_id,
            template=template,
            backend_adapter=backend_adapter,
            workspace_plan=workspace_plan,
            recovered_assignment=recovered_assignment,
        )
        if tenancy == SANDBOX_TENANCY_CONVERSATION
        else None
    )
    if conversation_pool_claim is not None:
        # The supplier has already removed this box from shared inventory and
        # adopted it to the Session's durable create assignment. Record the
        # exact cleanup address before any credential or workspace hand-off can
        # fail, then make the workspace mounted in that box this Session's
        # durable workspace.
        await manager.record_startup_allocation(
            session_id,
            SandboxAllocation(
                sandbox_id=conversation_pool_claim.sandbox_id,
                sandbox_backend=str(backend_adapter.name),
                scope="sandbox",
            ),
        )
        await claim_workspace_id(
            session_id,
            conversation_pool_claim.workspace_id,
        )

    vault_enabled = bool(
        getattr(load_astrabox_settings(), "sandbox_credential_vault_enabled", False)
    )
    mcp_vault_write = await resolve_mcp_credential_plan(
        manager,
        session_id=session_id,
        template=template,
        vault_enabled=vault_enabled,
    )
    mcp_hosts = tuple(sandbox_mcp_egress_hosts(getattr(template, "mcp_servers", None)))
    credential, network_policy, vault_write = resolve_model_credential_delivery(
        template=template,
        backend_adapter=backend_adapter,
        credential=request.credential,
        additional_vault_write=mcp_vault_write,
        required_hosts=(
            *tuple(request.required_network_hosts),
            *plugin_repo_egress_hosts(template),
        ),
        mcp_hosts=mcp_hosts,
        slot_id=(
            conversation_pool_claim.workload_id
            if conversation_pool_claim is not None
            else None
        ),
    )
    vault_write, runtime_env = await resolve_session_environment_credentials(
        manager,
        session_id=session_id,
        vault_enabled=vault_enabled,
        vault_write=vault_write,
        placeholder_context=(
            workload_placeholder_context(conversation_pool_claim.workload_id)
            if conversation_pool_claim is not None
            else None
        ),
    )

    # An engine that keeps its transcript to itself is mirrored by the platform,
    # not by its adapter: the adapter declares where the logs are, and every
    # engine that declares it gets the same treatment on the same seam. Composed
    # into the box's environment because the in-box relay reads it there, and
    # because this is where an engine's per-session facts already travel.
    session_log = _session_log_declaration(template)
    mirror_env: dict[str, str] = (
        transcript_mirror.mirror_env(manager, session_id, workspace_plan)
        if session_log is not None
        else {}
    )

    claimed = None
    if tenancy == SANDBOX_TENANCY_AGENT and recovered_assignment is None:
        try:
            claimed = await claim_prepared_engine_sandbox(
                manager,
                session_id=session_id,
                assignment_id=assignment_id,
                template=template,
                workspace_plan=workspace_plan,
                user_id=user_id,
                request=request,
                backend_adapter=backend_adapter,
                credential=credential,
                mcp_vault_write=mcp_vault_write,
                vault_enabled=vault_enabled,
                session_log=session_log,
            )
        except BaseException as exc:
            await _discard_failed_prepared_claim(
                manager,
                session_id=session_id,
                template=template,
                reason=(
                    "pre-activation handoff failed: "
                    f"{getattr(exc, 'code', None) or type(exc).__name__}"
                ),
            )
            raise
    if claimed is not None:
        return claimed

    if conversation_pool_claim is not None:
        from astrabox.core.service.orchestrator.agent.runtime_preparation import read_pool_runtime_receipt

        prepared = await read_pool_runtime_receipt(
            conversation_pool_claim.sandbox,
            sandbox_id=conversation_pool_claim.sandbox_id,
            workload_id=conversation_pool_claim.workload_id,
            generation=str(getattr(template, "runtime_generation", "") or ""),
        )
        prepared = {
            **prepared, "state": "claimed", "claimed_session_id": session_id,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }
        result = await claim_prepared_engine_sandbox(
            manager,
            session_id=session_id,
            assignment_id=assignment_id,
            template=template,
            workspace_plan=workspace_plan,
            user_id=user_id,
            request=request,
            backend_adapter=backend_adapter,
            credential=credential,
            mcp_vault_write=mcp_vault_write,
            vault_enabled=vault_enabled,
            session_log=session_log,
            prepared_claim=prepared,
            claimed_sandbox=conversation_pool_claim.sandbox,
        )
        if result is None:
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message="acquired prepared runtime could not be activated",
                status_code=409,
            )
        return result
    if (
        workspace_plan.subject_kind == "deployment_conversation"
        and tenancy == SANDBOX_TENANCY_AGENT
    ):
        # The Agent-shared tenancy: the conversation gets a place in a box,
        # not a box, and its engine service starts inside its own isolated
        # session. The box-create recipe below cannot express any of that,
        # so the placement path takes over here and rejoins at the
        # tenancy-agnostic tail (transcript restore, result assembly).
        sandbox, sandbox_id, runtime_identity, runner_uri = await _provision_shared_conversation(
            manager,
            backend_adapter,
            session_id=session_id,
            assignment_id=assignment_id,
            template=template,
            workspace_plan=workspace_plan,
            user_id=user_id,
            request=request,
            runtime_identity=dict(runtime_identity or {}),
            cwd=cwd,
            credential=credential,
            runtime_env=runtime_env,
            network_policy=network_policy,
            vault_write=vault_write,
            session_log=session_log,
            progress_callback=progress_callback,
            recover_assignment=recovered_assignment is not None,
        )
        return await _assemble_provisioned_sandbox(
            sandbox,
            manager=manager,
            backend_adapter=backend_adapter,
            sandbox_id=sandbox_id,
            session_id=session_id,
            template=template,
            workspace_plan=workspace_plan,
            runtime_identity=runtime_identity,
            cwd=cwd,
            credential=credential,
            session_log=session_log,
            mcp_vault_write=mcp_vault_write,
            vault_enabled=vault_enabled,
            runtime_env=runtime_env,
            runner_uri=runner_uri,
        )

    planned_mounts = await plan_workspace_mounts(
        subject_kind=str(getattr(workspace_plan, "subject_kind", "") or ""),
        session_id=session_id,
        agent_id=str(getattr(workspace_plan, "agent_id", "") or ""),
        assistant_id=str(getattr(workspace_plan, "assistant_id", "") or ""),
        user_id=str(getattr(workspace_plan, "user_id", "") or user_id or ""),
        runtime_identity=runtime_identity,
    )
    # The platform has already made every placement decision before it invokes
    # this capability.  A provider receives one complete, immutable box recipe;
    # it does not choose between reuse, pooling and creation on AstraBox's behalf.
    create_spec = engine_create_spec(
        session_id=session_id,
        assignment_id=assignment_id,
        template=template,
        request=request,
        cwd=cwd,
        credential=credential,
        mirror_env=mirror_env,
        runtime_env=runtime_env,
        network_policy=network_policy,
        permission_level=resolve_sandbox_permission_level(template),
        vault_write=vault_write,
        planned_mounts=planned_mounts,
        callback_url=callback_url,
    )
    sandbox = await create_sandbox_with_storage(backend_adapter, create_spec)
    # Recorded before the caller can fail: an unrecorded box that fails during
    # engine start is a leak nothing later can name.
    sandbox_id = extract_sandbox_id(sandbox)
    await manager.record_startup_allocation(
        session_id,
        SandboxAllocation(
            sandbox_id=sandbox_id,
            sandbox_backend=str(backend_adapter.name),
            scope="sandbox",
        ),
    )
    # Readiness, before the engine is started against it. A box whose durable
    # mounts did not arrive still boots, still answers, and writes everything to
    # a disk that dies with it — so the refusal has to happen here, while the
    # box is still just a box and nothing has been promised about its files.
    await workspace_is_ready(
        sandbox,
        planned_mounts,
        ref=workspace_ref_for_subject(
            subject_kind=str(getattr(workspace_plan, "subject_kind", "") or ""),
            agent_id=str(getattr(workspace_plan, "agent_id", "") or ""),
            assistant_id=str(getattr(workspace_plan, "assistant_id", "") or ""),
            session_id=session_id,
        ),
    )

    if progress_callback is not None:
        with contextlib.suppress(Exception):
            await progress_callback("mounting_nas")
    runtime_identity, cwd = await prepare_platform_workspace(
        manager,
        sandbox,
        session_id=session_id,
        template=template,
        workspace_plan=workspace_plan,
        user_id=user_id,
        runtime_identity=runtime_identity,
        cwd=cwd,
    )

    runner_uri = (
        await resolve_sandbox_websocket_endpoint(sandbox, int(request.wait_for_inbox_service_port))
        if request.wait_for_inbox_service_port is not None
        else None
    )
    return await _assemble_provisioned_sandbox(
        sandbox,
        manager=manager,
        backend_adapter=backend_adapter,
        sandbox_id=sandbox_id,
        session_id=session_id,
        template=template,
        workspace_plan=workspace_plan,
        runtime_identity=runtime_identity,
        cwd=cwd,
        credential=credential,
        session_log=session_log,
        mcp_vault_write=mcp_vault_write,
        vault_enabled=vault_enabled,
        runtime_env=runtime_env,
        runner_uri=runner_uri,
    )


@dataclass(frozen=True)
class ProvisionedSlotSandbox:
    """A box created under slot identity, before any Session exists."""

    sandbox: Any
    sandbox_id: str
    #: The directory the box starts in — the slot identity's workspace, which
    #: the claim re-points to its Session without moving.
    cwd: str
    #: The platform's name for the directory this box was given. Minted with the
    #: box because a slot has no conversation to read one from; the claim writes
    #: it onto the Session that takes the slot, and a box built to replace this
    #: one mounts that same name.
    workspace_id: str
    #: Whether the box's gateway binding substitutes this slot's placeholder,
    #: i.e. whether a claim must mint a Session key and replace the slot
    #: credential. Derived from the composed vault write, not asserted.
    gateway_substitution: bool
    model_credential: str
    runtime_env: Mapping[str, str] = field(default_factory=dict)
    environment_credential_contract: tuple[Mapping[str, Any], ...] = ()


async def provision_engine_slot_sandbox(
    manager: Any,
    *,
    slot_id: str,
    template: Any,
    runtime_identity: dict[str, Any],
    request: EngineSandboxRequest,
) -> ProvisionedSlotSandbox:
    """Create one engine's box for a prepared slot from the same declaration.

    The differences from :func:`provision_engine_sandbox` are exactly the
    Session-bound residues a preparation must not freeze:

    * the create's ``session_id`` metadata carries the SLOT id — a slot never
      impersonates a platform Session, and the claim re-points the box's
      metadata to its Session before recording the allocation;
    * no startup allocation is recorded — there is no Session row to own the
      box yet; the claim records it (``record_startup_allocation``);
    * a transcript-mirroring engine's box gets the deferred target file
      (:func:`transcript_mirror.deferred_mirror_env`) instead of per-session
      mirror values, and the claim writes the target before the engine
      conversation exists;
    * the model credential is the slot's own placeholder with a per-slot vault
      substitution behind it (see :func:`resolve_model_credential_delivery`),
      so the claim can re-point the child's gateway identity without touching
      the box;
    * resume never reaches this path — a prepared box is for new
      conversations only (a resume names its own durable engine session).

    The returned box is untracked by any Session; the CALLER owns destroying
    it on any failure before its manifest is published.
    """

    target_slot = str(slot_id or "").strip()
    if not target_slot:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="engine slot provisioning requires a slot id",
            status_code=500,
        )
    composed = {request.credential_env_var, request.cwd_env_var} - {None}
    conflicting = sorted(composed & set(request.env))
    if conflicting:
        raise RuntimeError(
            f"engine env must not set {conflicting}: the platform composes "
            "those, so a value declared here would either disagree with the "
            "sidecar's real credential or name a directory the box is not in"
        )

    from astrabox.seams.sandbox import sandbox_for_template

    # The template's backend directly: backend persistence lives on Session
    # rows, and a slot has none — the claim's allocation record is what later
    # makes the box resolvable by id.
    backend_adapter = sandbox_for_template(template)
    if not bool(getattr(backend_adapter, "supports_correlated_create", False)):
        raise APIError(
            code="SANDBOX_CORRELATED_CREATE_UNSUPPORTED",
            message=(
                f"sandbox backend {backend_adapter.name!r} cannot recover a "
                "prepared create after its worker exits"
            ),
            status_code=501,
        )

    cwd = (
        str(request.cwd or "").strip()
        or str((runtime_identity or {}).get("workspace_dir") or "").strip()
    )
    if not cwd:
        raise RuntimeError(f"engine slot sandbox for slot {target_slot!r} has no working directory")

    vault_enabled = bool(
        getattr(load_astrabox_settings(), "sandbox_credential_vault_enabled", False)
    )
    mcp_vault_write = await resolve_agent_mcp_credential_plan(
        template=template,
        vault_enabled=vault_enabled,
    )
    credential, network_policy, vault_write = resolve_model_credential_delivery(
        template=template,
        backend_adapter=backend_adapter,
        credential=request.credential,
        additional_vault_write=mcp_vault_write,
        required_hosts=(
            *tuple(request.required_network_hosts),
            *plugin_repo_egress_hosts(template),
        ),
        mcp_hosts=tuple(sandbox_mcp_egress_hosts(getattr(template, "mcp_servers", None))),
        slot_id=target_slot,
    )
    vault_write, runtime_env = await resolve_prepared_environment_credentials(
        template,
        slot_id=target_slot,
        vault_enabled=vault_enabled,
        vault_write=vault_write,
    )

    session_log = _session_log_declaration(template)
    mirror_env: dict[str, str] = (
        transcript_mirror.deferred_mirror_env() if session_log is not None else {}
    )

    # A prepared whole box already has one conversation-shaped identity: the
    # temporary slot. Mount exactly that identity's workspace, under a fresh
    # platform workspace id. Claim later changes ownership, not layout; a cold
    # replacement can therefore mount the same ``.../workspace`` subpath at the
    # same box path. Mounting an Agent-wide home root here would create a layout
    # that only a shared Agent box understands and make replacement lose files.
    slot_workspace_id = mint_workspace_id()
    planned_mounts = await plan_workspace_mounts(
        subject_kind="deployment_conversation",
        session_id="",
        workspace_id=slot_workspace_id,
        agent_id=str(getattr(template, "agent_id", "") or ""),
        assistant_id=None,
        user_id=None,
        runtime_identity=runtime_identity,
    )
    resource_limits, resource_requests = sandbox_create_resources()
    sandbox = await create_sandbox_with_storage(
        backend_adapter,
        SandboxCreateSpec(
            session_id=target_slot,
            assignment_id=target_slot,
            resource_limits=resource_limits,
            resource_requests=resource_requests,
            image=resolve_runtime_template_name(template),
            entrypoint=tuple(request.entrypoint),
            cwd=cwd,
            env={
                **SANDBOX_SELF_DESCRIPTION,
                **dict(request.env),
                **mirror_env,
                **runtime_env,
                **({request.credential_env_var: credential} if request.credential_env_var else {}),
                **({request.cwd_env_var: cwd} if request.cwd_env_var else {}),
            },
            publish_ports=tuple(request.publish_ports),
            wait_for_inbox_service_port=request.wait_for_inbox_service_port,
            # Every box the platform hands to a conversation has commands run
            # in it before its engine starts — the mount check, the workspace
            # provisioning, the identity script. Proving the channel at create
            # puts that failure where the box is still just a box, instead of
            # in the first thing that tried to use it.
            requires_command_channel=True,
            death_callback_url=None,
            network_policy=network_policy,
            permission_level=resolve_sandbox_permission_level(template),
            vault_write=vault_write,
            workspace_mounts=planned_mounts,
        )
    )
    # A slot that reaches the pool without its mount is worse than a slot that
    # was never prepared: it is lent out as ready, and every conversation that
    # borrows it writes where nothing will look.
    #
    # Destroying on refusal is this function's job rather than its caller's. No
    # startup allocation is recorded for a slot — there is no Session row to own
    # the box until a claim records one — so between the create above and the
    # return below the box is named by nothing, and a caller's `try` that begins
    # after the return cannot reach it.
    try:
        await workspace_is_ready(
            sandbox,
            planned_mounts,
            ref=workspace_ref_for_subject(
                subject_kind="deployment_conversation",
                agent_id=str(getattr(template, "agent_id", "") or ""),
                assistant_id=None,
                session_id=target_slot,
            ),
        )
    except BaseException:
        from astrabox.core.service.orchestrator.agent.prepared_boxes import (
            destroy_slot_box,
        )

        await destroy_slot_box(
            extract_sandbox_id(sandbox),
            sandbox_backend=str(backend_adapter.name),
            slot_id=target_slot,
        )
        raise
    return ProvisionedSlotSandbox(
        workspace_id=slot_workspace_id,
        sandbox=sandbox,
        sandbox_id=extract_sandbox_id(sandbox),
        cwd=cwd,
        gateway_substitution=vault_write is not None,
        model_credential=credential,
        runtime_env=runtime_env,
        environment_credential_contract=tuple(
            environment_credential_contract(vault_write)
        ),
    )
