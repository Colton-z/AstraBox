"""Hermes engine adapter using its TUI JSON-RPC protocol over OpenSandbox PTY.

Each (user, assistant) profile owns one resident ``tui_gateway.entry`` process
inside the Assistant's sandbox — the vendor's own multi-session runtime — and
every conversation of that profile is one ``session.create``/``session.resume``
on it (decision record: ``docs/maintainers/hermes-runtime-preparation.md``).
The host reaches the gateway through OpenSandbox's execd endpoint; Hermes does
not expose an application port or receive a platform control key. Model
credentials follow the separately configured outbound Vault path.

Preparation for this engine is therefore the profile's resident gateway, not a
prepared slot: the Agent-row slot machinery
(``agent/prepared_slots.py``) is keyed by an Agent and fingerprints anonymous,
unclaimed units, while every Hermes unit is owned by its profile from birth
(home path, workload account, MCP deployment id all embed that identity), so
the adapter keeps the default ``prepare_runtime`` refusal from ``EngineAdapter``.
"""

from __future__ import annotations

import asyncio
import contextlib
import base64
import hashlib
import json
import posixpath
from urllib.parse import quote, urlsplit
import re
import shlex
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.config.release_images import release_image
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineCapabilityManifest,
    EngineClient,
    EngineConversationBinding,
    EngineKind,
    EngineStartupContext,
    EngineStartupMaterialRequest,
    initialize_engine_client,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.engine.registry import (
    register_engine_adapter,
)
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesTuiEngineClient,
    decode_turn_anchor,
)
from astrabox.core.service.orchestrator.engine.hermes_gateway import (
    gateway_handle_for_sandbox,
    resolve_gateway_handle,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    EXECD_PORT,
    resolve_sandbox_endpoint,
)
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.runtime.sandbox_script_writer import (
    install_verified_text_script,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
)
from astrabox.seams.model import ResolvedModelAccess
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    runtime_mcp_servers_for_binding,
    template_mcp_servers,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    normalize_runtime_identity,
    plan_assistant_profile_identity,
    run_sandbox_command,
)
from astrabox.core.service.orchestrator.runtime.storage import (
    _normalize_deploy_private_key,
)


HERMES_RUNTIME_IMAGE_COMPONENT = "sandbox-hermes"

logger = get_logger(__name__)

HERMES_SKILL_REPO_CACHE_DIR = "/opt/astrabox/hermes-skill-repos"
_HERMES_PROFILE_SETUP_SCRIPT_PATH = "/usr/local/bin/astrabox-hermes-profile-setup"
_HERMES_PROFILE_ENV_FILENAME = "astrabox-hermes.env"
_HERMES_RUNTIME_STATE_FILENAME = "astrabox-runtime-state.json"
_HERMES_STATE_BOOTSTRAP = "/opt/astrabox/hermes/hermes_state_bootstrap.py"
_HERMES_SKILL_REPO_CACHE_SCRIPT_PATH = "/usr/local/bin/astrabox-hermes-skill-repo-cache"
_HERMES_CONFIG_MERGE_SCRIPT_PATH = "/usr/local/bin/astrabox-hermes-config-merge"
_HERMES_CUSTOM_PROVIDER_NAME = "AstraBox"
_HERMES_DEFAULT_API_MODE = "chat_completions"

# The model API key value still needs to land as a discrete env var because
# the overwrite blob's `custom_providers[].key_env` references this name; the
# Hermes model client reads os.environ[key_env] when it sends a request. In
# protected mode this value is only the non-secret placeholder.
#
# That vendor seam is also why model auth for a resident gateway cannot ride a
# per-workload placeholder the way a prepared Claude slot's does
# (``egress_credentials.workload_model_placeholder``): Hermes resolves ONE
# ``os.environ[key_env]`` per process from its config-level
# ``custom_providers`` — the environment is fixed at spawn and shared by every
# session in the process, so there is no per-session credential to substitute.
# Gateway spend and tracing attribution for Hermes is therefore per
# (user, assistant) profile, not per conversation, and that limit is a vendor
# constraint to state (docs/maintainers/hermes-runtime-preparation.md), not a
# gap to paper over.
_HERMES_MODEL_API_KEY_ENV = "ASTRABOX_HERMES_MODEL_API_KEY"

# Generic profile envs consumed by scripts/runtime/hermes_config_merge.py
# inside the runtime image. Platform pushes JSON blobs/base64 text; bootstrap
# materializes profile-owned config.yaml, skills, and SOUL.md before TUI start.
_HERMES_CONFIG_OVERWRITE_ENV = "ASTRABOX_HERMES_CONFIG_OVERWRITE"
_HERMES_CONFIG_DEFAULTS_ENV = "ASTRABOX_HERMES_CONFIG_DEFAULTS"
_HERMES_SKILL_SOURCE_DIRS_ENV = "ASTRABOX_HERMES_SKILL_SOURCE_DIRS"
_HERMES_SOUL_B64_ENV = "ASTRABOX_HERMES_SOUL_B64"
_HERMES_CRON_JOBS_B64_ENV = "ASTRABOX_HERMES_CRON_JOBS_B64"

# Generic plugin passthrough. A Hermes plugin (a memory provider such as
# OpenViking, or any other) is pure configuration the operator supplies in the
# template's ``model_config.hermes.plugins`` — the engine core knows no plugin
# by name. ``_PLUGIN_FILES_B64`` carries the {relative-path: json} config files
# the profile setup writes under the profile home. Plugin environment variables
# are written directly into the profile's private launcher env file.
_HERMES_PLUGIN_FILES_B64_ENV = "ASTRABOX_HERMES_PLUGIN_FILES_B64"


_HERMES_PROFILE_SETUP_SCRIPT = (
    Path(__file__).parents[1] / "runtime" / "hermes-profile-setup"
).read_text(encoding="utf-8")

# Execd may take a few seconds after container start to accept commands. Poll it
# after the sandbox handle is tracked — not folded into create_sandbox — so a
# readiness timeout reaches start_runtime's cleanup path and the just-started
# container is killed rather than orphaned.
_HERMES_INBOX_READY_TIMEOUT_S = 90


async def _await_hermes_inbox_ready(
    sandbox: Any, *, timeout_s: int | None = None
) -> None:
    """Block until the box's command server accepts an exec, or fail loud.

    Uses the neutral ``commands.run`` seam, backed by OpenSandbox execd for this
    provider. ``timeout_s`` defaults to the module constant, resolved at call
    time.

    The per-probe timeout goes through :func:`run_sandbox_command` rather than
    onto ``commands.run`` directly. Adapters disagree about how a timeout is
    spelled, and the SDK's own ``CommandsAdapter.run`` takes only
    ``opts=RunCommandOpts(...)`` — passing ``timeout_in_millis`` to it raises
    ``TypeError`` on every probe, so a loop that called it directly would never
    see a ready box and would always spend the full deadline before failing.
    """
    effective_s = _HERMES_INBOX_READY_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + float(effective_s)
    last_exc: BaseException | None = None
    while True:
        try:
            await run_sandbox_command(
                getattr(getattr(sandbox, "commands", None), "run", None),
                "true",
                timeout_in_millis=5000,
            )
            return
        except Exception as exc:  # noqa: BLE001 — box still booting; retry to deadline
            last_exc = exc
        if time.monotonic() >= deadline:
            raise APIError(
                code="SANDBOX_INBOX_SERVER_UNREADY",
                message=(
                    f"hermes command service did not become ready within "
                    f"{effective_s}s: {last_exc}"
                ),
                status_code=502,
            )
        await asyncio.sleep(1.0)


class HermesEngineClient(HermesTuiEngineClient):
    """Public in-tree name for the official Hermes TUI JSON-RPC client."""


class HermesEngineAdapter(EngineAdapter):
    """EngineAdapter for Hermes — declares and connects its gateway client."""

    def child_run_is_active(self, child_run: dict[str, Any]) -> bool:
        from astrabox.core.service.orchestrator.engine.child_runs import (
            ChildRunProjectionError,
        )
        from astrabox.core.service.orchestrator.engine.hermes_client import (
            _HERMES_CHILD_RUN_OPEN_EVENTS,
            _HERMES_CHILD_RUN_UPDATE_EVENTS,
        )

        if child_run.get("closed") is True:
            return False
        event = child_run.get("engine_event")
        if event == "subagent.complete":
            return False
        if event in _HERMES_CHILD_RUN_OPEN_EVENTS | _HERMES_CHILD_RUN_UPDATE_EVENTS:
            return True
        raise ChildRunProjectionError(f"Hermes child has unknown event {event!r}")

    @property
    def engine_kind(self) -> EngineKind:
        return "assistant"

    @property
    def engine_client_type(self) -> type[EngineClient]:
        return HermesEngineClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        # Hermes runs one long-lived per-(user, assistant) gateway workspace
        # that many sessions attach to, drives turns through its own
        # EngineClient, fences + recovers turns natively at the gateway, and
        # lays its home config under ``.hermes``.
        return EngineRuntimeCapabilities(
            engine_kind="assistant",
            supported_session_kinds=frozenset({"assistant_chat"}),
            workload=EngineWorkloadDeclaration(
                config_dir_name=".hermes",
                config_env_var="HERMES_HOME",
                required_commands=(
                    "bash",
                    "getent",
                    "runuser",
                    "id",
                    "mkdir",
                    "chown",
                    "chmod",
                    "ln",
                    "readlink",
                    "hermes",
                    "/usr/local/bin/astrabox-provision-conversation",
                    "/usr/local/bin/astrabox-hermes-profile-setup",
                ),
            ),
            conversation_placement=CONVERSATION_PLACEMENT_PER_ACCOUNT,
            default_runtime_image=release_image(HERMES_RUNTIME_IMAGE_COMPONENT),
            # Hermes renders platform MCP bindings into its native config.
            # Its skills use Hermes-owned source repositories instead of the
            # Agent/Assistant skill list, and it has no Claude plugin format.
            configuration_inputs=frozenset({"mcp_servers"}),
        )

    def sandbox_request(
        self,
        *,
        template: Any,
        model_access: Any,
    ) -> EngineSandboxRequest:
        """Declare the Hermes box; platform code decides when and where it exists."""

        return EngineSandboxRequest(
            entrypoint=("/opt/gem/run.sh",),
            credential=_hermes_credential_request(model_access),
            credential_env_var=None,
            cwd=_HERMES_WORKSPACE_ROOT,
            env=_build_hermes_sandbox_env(),
        )

    def startup_material_request(
        self,
        *,
        template: Any,
        model_access: Any,
        deployment_settings: Any,
    ) -> EngineStartupMaterialRequest:
        """Declare repository keys for platform resolution before box setup."""

        _ = template
        repos = _get_hermes_skill_repos(
            deployment_settings,
            dict(model_access.configuration),
        )
        names: list[str] = []
        for index, repo in enumerate(repos):
            name = str(repo.get("deploy_key_secret_name") or "").strip()
            if not name:
                raise APIError(
                    code="HERMES_SKILL_REPO_MISSING_KEY",
                    message=(
                        f"hermes_skill_repos[{index}].deploy_key_secret_name "
                        "is required for ssh protocol"
                    ),
                    status_code=500,
                )
            if name not in names:
                names.append(name)
        return EngineStartupMaterialRequest(
            secret_names=tuple(names), runtime_state_store=True
        )

    async def activate_runtime(
        self,
        context: EngineStartupContext,
    ) -> SessionRuntime:
        """Prepare Hermes's native profile in an already assigned box."""

        template = context.template
        workspace_plan = context.workspace_plan
        user_id = context.user_id
        conversation_user_id = str(
            getattr(workspace_plan, "user_id", None) or ""
        ).strip() or None
        profile_ref = _resolve_hermes_profile_ref(
            user_id=user_id,
            assistant_id=workspace_plan.assistant_id,
        )
        sandbox = context.sandbox

        engine_client: HermesEngineClient | None = None
        engine_manifest: EngineCapabilityManifest | None = None
        runtime_identity = context.runtime_identity
        try:
            await _await_hermes_inbox_ready(sandbox)
            if not runtime_identity:
                raise APIError(
                    code="HERMES_PROFILE_INVALID",
                    message="platform startup supplied no Assistant runtime identity",
                    status_code=500,
                )
            spawn_fingerprint = None
            if context.attach_mode != "lightweight":
                spawn_fingerprint = await _prepare_hermes_profile(
                    sandbox,
                    identity=runtime_identity,
                    deployment_settings=context.deployment_settings,
                    template=template,
                    model_access=context.model_access,
                    model_api_key=context.model_credential,
                    user_id=profile_ref["user_id"],
                    conversation_user_id=conversation_user_id,
                    assistant_id=profile_ref["assistant_id"],
                    runtime_env=dict(context.runtime_env or {}),
                    mcp_deployment_id=context.platform_mcp_deployment_id,
                    platform_secrets=context.platform_secrets,
                    runtime_state_store=context.runtime_state_store,
                )
            engine_client = await self._start_engine(
                sandbox,
                session_id=context.session_id,
                identity=runtime_identity,
                resume_session_key=context.resume_session_key,
                profile_ref=profile_ref,
                spawn_fingerprint=spawn_fingerprint,
            )
            engine_manifest = await initialize_engine_client(
                engine_client,
                expected_engine_kind="assistant",
                conversation_binding=EngineConversationBinding(
                    platform_session_id=context.session_id,
                    engine_session_key=context.resume_session_key,
                ),
            )
        except BaseException as exc:
            if engine_client is not None:
                with contextlib.suppress(BaseException):
                    await engine_client.close()
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"hermes runtime start failed: {exc}",
                status_code=502,
            ) from exc

        assert engine_client is not None
        assert engine_manifest is not None
        return SessionRuntime(
            session_id=context.session_id,
            agent=None,
            sandbox=sandbox,
            sandbox_id=extract_sandbox_id(sandbox),
            engine_session_key=context.resume_session_key,
            terminal_cwd=context.cwd,
            permission_mode=None,
            engine_kind="assistant",
            engine_client=engine_client,
            engine_manifest=engine_manifest,
            conversation_bound=True,
            runtime_identity=runtime_identity,
            prepare_engine_input=context.prepare_engine_input,
        )

    async def _start_engine(
        self,
        sandbox: Any,
        *,
        session_id: str,
        identity: dict[str, Any] | None = None,
        resume_session_key: str | None = None,
        profile_ref: dict[str, str] | None = None,
        spawn_fingerprint: str | None = None,
    ) -> EngineClient:
        """Resolve the profile's resident gateway and open one session on it.

        The gateway spawn (with the vendor's ``gateway.ready`` barrier) is the
        engine's preparation cost and runs here at most once per profile per
        box; every later conversation of the profile finds the resident
        gateway and pays only ``session.create``/``session.resume``.
        ``spawn_fingerprint`` names the profile configuration just written
        (the callers that rewrite it pass the value from
        :func:`_prepare_hermes_profile`, which is also where a backend running
        under older content is restarted, since reconnecting cannot correct
        one). Here it only fences this host's attachment: one established
        under the old content is dropped rather than kept for new sessions.
        ``None`` accepts the standing resident, for attaches that do not touch
        the profile.
        """

        normalized = normalize_runtime_identity(identity)
        if normalized is None:
            raise APIError(
                code="HERMES_PROFILE_INVALID",
                message="Hermes TUI Gateway requires a provisioned runtime identity",
                status_code=500,
            )
        if not profile_ref or not str(profile_ref.get("profile_key") or "").strip():
            raise APIError(
                code="HERMES_PROFILE_INVALID",
                message="Hermes gateway resolution requires the Assistant profile reference",
                status_code=500,
            )
        sandbox_id = extract_sandbox_id(sandbox) or str(
            normalized.get("sandbox_id") or ""
        ).strip()
        if not sandbox_id:
            raise APIError(
                code="HERMES_PROFILE_INVALID",
                message="Hermes gateway resolution requires the sandbox id",
                status_code=500,
            )
        # The backend is already running: `astrabox-hermes-serve` starts it
        # under supervisord and `astrabox-hermes-forward` publishes it once it
        # answers, so this resolves an address rather than starting anything.
        endpoint = await resolve_sandbox_endpoint(sandbox, HERMES_BACKEND_PORT)
        gateway = await resolve_gateway_handle(
            url=hermes_backend_ws_url(_hermes_backend_token(normalized)),
            dial=hermes_backend_dial(endpoint.origin),
            headers=endpoint.headers,
            sandbox_id=sandbox_id,
            profile_key=profile_ref["profile_key"],
            spawn_fingerprint=spawn_fingerprint,
        )
        return HermesEngineClient(
            gateway=gateway,
            platform_session_id=session_id,
            resume_session_key=resume_session_key,
        )

    async def quiesce_and_save_runtime_state(
        self, sandbox: Any, *, runtime_identity: dict[str, Any]
    ) -> None:
        run_fn = getattr(getattr(sandbox, "commands", None), "run", None)
        phase = "hermes_stop_state_mirror"
        try:
            for program, phase in (
                ("astrabox-hermes-state-mirror", "hermes_stop_state_mirror"),
                (_HERMES_BACKEND_PROGRAM, "hermes_stop_backend"),
            ):
                # Re-entering a failed save can find an already stopped service.
                # The supervisor's read-back, not its stop command text, is proof.
                await run_sandbox_command(
                    run_fn, "supervisorctl stop " + shlex.quote(program)
                )
                status = await run_sandbox_command(
                    run_fn,
                    "bash -lc " + shlex.quote(
                        "status=0; supervisorctl status " + shlex.quote(program)
                        + '; status=$?; if [ "$status" -ne 0 ] '
                        '&& [ "$status" -ne 3 ]; then exit "$status"; fi'
                    ),
                )
                if (
                    getattr(status, "error", None)
                    or getattr(status, "exit_code", None) != 0
                    or re.fullmatch(
                        re.escape(program) + r"\s+STOPPED(?:\s+[^\n]*)?",
                        _hermes_command_output(status).strip(),
                    ) is None
                ):
                    raise RuntimeError("supervisor did not confirm STOPPED")
            phase = "hermes_native_state_save"
            profile_env = posixpath.join(
                runtime_identity["config_dir"], _HERMES_PROFILE_ENV_FILENAME
            )
            save_script = (
                'set -euo pipefail; source "$1"; '
                'test "$ASTRABOX_HERMES_PROFILE_LINUX_USER" = "$2"; '
                'test "$ASTRABOX_HERMES_PROFILE_HOME" = "$3"; '
                'exec "$HERMES_VENV/bin/python" '
                '/opt/astrabox/hermes/hermes_state_mirror.py save --profile-env "$1"'
            )
            command = shlex.join([
                "runuser", "-u", runtime_identity["linux_user"], "--", "env",
                f"HOME={runtime_identity['home_dir']}",
                f"USER={runtime_identity['linux_user']}",
                f"LOGNAME={runtime_identity['linux_user']}",
                "bash", "--noprofile", "--norc", "-c", save_script,
                "astrabox-hermes-state-save", profile_env,
                runtime_identity["linux_user"], runtime_identity["home_dir"],
            ])
            result = await run_sandbox_command(run_fn, command)
            markers = [
                line for line in _hermes_command_output(result).splitlines()
                if re.fullmatch(r"HERMES_STATE_SAVE_COMPLETE snapshot_id=\S+", line)
            ]
            if (
                getattr(result, "error", None)
                or getattr(result, "exit_code", None) != 0
                or len(markers) != 1
            ):
                raise RuntimeError("native state save did not confirm its snapshot")
        except Exception as exc:
            raise APIError(
                code="ASSISTANT_WORKSPACE_CONVERGENCE_FAILED",
                message=f"Hermes runtime state barrier failed during {phase}",
                status_code=503,
                data={"phase": phase},
            ) from exc

    def process_disposal(self) -> Any:
        return _HERMES_PROCESS_DISPOSAL

# The shared assistant workspace root baked into the Hermes runtime image (WORKDIR).
# Per-(user, assistant) profile homes live under it; used as the container cwd when
# the workspace plan carries none.
_HERMES_WORKSPACE_ROOT = "/home/conversations"


def _resolve_hermes_profile_ref(
    *,
    user_id: str | None,
    assistant_id: str | None,
) -> dict[str, str]:
    safe_user_id = _safe_hermes_profile_segment(user_id, label="user_id")
    safe_assistant_id = _safe_hermes_profile_segment(
        assistant_id,
        label="assistant_id",
    )
    return {
        "user_id": safe_user_id,
        "assistant_id": safe_assistant_id,
        "profile_key": f"{safe_user_id}:{safe_assistant_id}",
    }


def _safe_hermes_profile_segment(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9._@+:-]+", text):
        raise APIError(
            code="HERMES_PROFILE_INVALID",
            message=f"unsafe hermes profile {label}: {text!r}",
            status_code=500,
        )
    return text


def _build_hermes_workspace_identity(
    *,
    user_id: str,
    assistant_id: str,
    sandbox_id: str | None,
) -> dict[str, Any]:
    profile_key = f"{user_id}:{assistant_id}"
    identity = plan_assistant_profile_identity(
        engine_kind="assistant",
        user_id=user_id,
        assistant_id=assistant_id,
        sandbox_id=sandbox_id,
    )
    identity["stage_evidence"] = {
        "hermes_profile_key": profile_key,
        "hermes_control_transport": "opensandbox_execd_pty",
        "hermes_control_port": EXECD_PORT,
    }
    return identity


async def _prepare_hermes_profile(
    sandbox: Any,
    *,
    identity: dict[str, Any],
    deployment_settings: Any,
    template: Any,
    model_access: ResolvedModelAccess,
    model_api_key: str,
    user_id: str,
    conversation_user_id: str | None,
    assistant_id: str,
    runtime_state_store: dict[str, Any] | None,
    runtime_env: dict[str, str] | None = None,
    mcp_deployment_id: str | None = None,
    platform_secrets: dict[str, str] | None = None,
) -> str:
    """Materialize the profile in the box; return its spawn fingerprint.

    The fingerprint hashes what fixes the resident backend's behaviour at
    start: the profile env file's content — model route and key identity,
    config blobs, plugin env, skill sources, SOUL, cron. Hermes reads all of it
    once, when it constructs the agent, so a backend already running under
    older content cannot be corrected by reconnecting to it. This function is
    therefore where a change takes effect: it compares before writing and
    restarts the supervised backend when the content moved. The fingerprint's
    remaining job is downstream — an attachment established under the old
    content must not keep gaining sessions under the new one.
    """

    model_config_payload = dict(model_access.configuration)
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        raise APIError(
            code="HERMES_PROFILE_INVALID",
            message="hermes profile identity is missing required fields",
            status_code=500,
        )
    command_runner = getattr(sandbox, "commands", None)
    run_fn = getattr(command_runner, "run", None) if command_runner is not None else None
    if not callable(run_fn):
        raise APIError(
            code="HERMES_PROFILE_UNSUPPORTED",
            message="sandbox command runner is required to prepare the Hermes profile",
            status_code=502,
        )

    repo_source_dirs = await _prepare_hermes_skill_repos(
        sandbox,
        deployment_settings=deployment_settings,
        model_config_payload=model_config_payload,
        platform_secrets=dict(platform_secrets or {}),
    )
    overwrite_blob = _build_hermes_config_overwrite(
        deployment_settings,
        template=template,
        model_access=model_access,
        mcp_deployment_id=mcp_deployment_id,
    )
    # Operator-configured plugins (a memory provider such as OpenViking, or any
    # other) are pure config in the template; the engine passes them through
    # generically with the conversation's runtime identity substituted in.
    plugins = _build_hermes_plugins(
        model_config_payload,
        runtime_vars={
            "conversation_user_id": str(conversation_user_id or "").strip(),
            "assistant_id": str(assistant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
        },
    )
    defaults_blob = _build_hermes_config_defaults(
        plugin_config_defaults=plugins.config_defaults,
    )
    skill_source_dirs = _build_hermes_skill_source_dirs(
        deployment_settings,
        model_config_payload=model_config_payload,
        repo_source_dirs=repo_source_dirs,
    )
    soul_content = _build_hermes_soul_content(model_config_payload)
    cron_jobs_payload = _build_hermes_cron_jobs_payload(model_config_payload)
    normalized_model_api_key = str(model_api_key or "").strip()
    if not normalized_model_api_key:
        raise APIError(
            code="HERMES_MODEL_API_KEY_NOT_CONFIGURED",
            message="model api key not configured for hermes template",
            status_code=500,
        )

    env = {
        _HERMES_CONFIG_OVERWRITE_ENV: json.dumps(
            overwrite_blob, ensure_ascii=False, sort_keys=True
        ),
        _HERMES_CONFIG_DEFAULTS_ENV: json.dumps(
            defaults_blob, ensure_ascii=False, sort_keys=True
        ),
        _HERMES_MODEL_API_KEY_ENV: normalized_model_api_key,
        **plugins.env,
        "HERMES_HOME": normalized["config_dir"],
        **dict(runtime_env or {}),
    }
    if skill_source_dirs:
        env[_HERMES_SKILL_SOURCE_DIRS_ENV] = json.dumps(
            skill_source_dirs, ensure_ascii=False
        )
    if soul_content:
        env[_HERMES_SOUL_B64_ENV] = base64.b64encode(
            soul_content.encode("utf-8")
        ).decode("ascii")
    env[_HERMES_CRON_JOBS_B64_ENV] = base64.b64encode(
        json.dumps(cron_jobs_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    if plugins.config_files:
        plugin_files_json = json.dumps(
            plugins.config_files, ensure_ascii=True, sort_keys=True
        )
        env[_HERMES_PLUGIN_FILES_B64_ENV] = base64.b64encode(
            plugin_files_json.encode("utf-8")
        ).decode("ascii")
    env.update(
        {
            "ASTRABOX_HERMES_PROFILE_LINUX_USER": normalized["linux_user"],
            "ASTRABOX_HERMES_PROFILE_HOME": normalized["home_dir"],
            "ASTRABOX_HERMES_WORKSPACE": normalized["workspace_dir"],
            # The credential the in-box backend checks a WebSocket upgrade
            # against, injected the way Hermes' own desktop shell injects it.
            # Without it `hermes serve` mints a random one per start, which no
            # caller could then present.
            #
            # Derived from the profile rather than drawn fresh so that a
            # backend restarted by supervisord keeps the credential the host
            # already holds; it never leaves the box or the platform's own
            # store, and the file it lives in is mode 600 under a 700 profile.
            "HERMES_DASHBOARD_SESSION_TOKEN": _hermes_backend_token(normalized),
        }
    )

    profile_env_path = posixpath.join(
        normalized["config_dir"],
        _HERMES_PROFILE_ENV_FILENAME,
    )
    profile_env_content = _build_hermes_profile_env_exports(env)
    # The identity of the CONFIGURATION the backend is serving: this file and
    # nothing else. The launcher command is deliberately not part of it — the
    # image owns that (`astrabox-hermes-serve`), so hashing it would hash a
    # constant. What the fence enforces is that an attachment established under
    # different content must not keep gaining sessions under the new one.
    spawn_fingerprint = hashlib.sha256(
        profile_env_content.encode("utf-8")
    ).hexdigest()
    standing = await _hermes_profile_env_standing(
        run_fn, path=profile_env_path, digest=spawn_fingerprint
    )
    if not runtime_state_store:
        raise APIError(
            code="HERMES_PROFILE_INVALID",
            message="platform startup supplied no Hermes runtime-state target",
            status_code=500,
        )
    await install_verified_text_script(
        sandbox,
        path=posixpath.join(normalized["config_dir"], _HERMES_RUNTIME_STATE_FILENAME),
        content=json.dumps(runtime_state_store, ensure_ascii=False, sort_keys=True),
        mode=0o600,
        error_code="HERMES_PROFILE_ENV_INSTALL_FAILED",
        error_message="failed to install Hermes runtime-state target",
    )
    await _install_hermes_profile_env_file(
        sandbox,
        path=profile_env_path,
        content=profile_env_content,
    )
    result = await run_fn(
        "bash -lc "
        + shlex.quote(
            'set -euo pipefail; source "$1"; exec bash "$2" "$3"'
        )
        + " _ "
        + shlex.quote(profile_env_path)
        + " "
        + shlex.quote(_HERMES_PROFILE_SETUP_SCRIPT_PATH)
        + " "
        + shlex.quote(normalized["workspace_source_dir"])
    )
    error = getattr(result, "error", None)
    output = _hermes_command_output(result)
    if error or "HERMES_PROFILE_READY" not in output:
        raise APIError(
            code="HERMES_PROFILE_SETUP_FAILED",
            message=(
                "failed to prepare Hermes profile: "
                f"{error or 'missing readiness marker'}; output={output[:2000]!r}"
            ),
            status_code=502,
        )
    sandbox_id = extract_sandbox_id(sandbox)
    if not sandbox_id:
        raise APIError(
            code="HERMES_PROFILE_INVALID",
            message="Hermes profile initialization requires the actual sandbox id",
            status_code=500,
        )
    initialized = await run_fn(
        "bash -lc " + shlex.quote(
            'set -euo pipefail; exec "$HERMES_VENV/bin/python" "$1" publish '
            '--profile-env "$2" --sandbox-id "$3"'
        ) + " _ " + shlex.quote(_HERMES_STATE_BOOTSTRAP)
        + " " + shlex.quote(profile_env_path) + " " + shlex.quote(sandbox_id)
    )
    if getattr(initialized, "error", None) or "HERMES_PROFILE_INITIALIZED" not in _hermes_command_output(initialized):
        raise APIError(
            code="HERMES_PROFILE_SETUP_FAILED",
            message="failed to publish this box's Hermes profile initialization",
            status_code=502,
        )
    if standing == "CHANGED" and "fresh=0" in _hermes_command_output(initialized):
        await _restart_hermes_backend(run_fn)
    return spawn_fingerprint


#: The port `astrabox-hermes-forward` publishes the backend on. The backend
#: itself binds loopback (see `astrabox-hermes-serve` for why); this is the
#: one the sandbox's endpoint face knows about.
HERMES_BACKEND_PORT = 9118

#: Where `hermes serve` binds inside the box, as the `Host` header of an
#: upgrade must name it. Not an address reachable from here: the bind is
#: loopback on purpose and `astrabox-hermes-forward` is what publishes it.
#: Only the host half is load-bearing — Hermes strips the port before
#: comparing — so this does not have to be kept in step with the box's
#: `ASTRABOX_HERMES_LOOPBACK_PORT`; it names the real one because a URL that
#: named a different port would read as a mistake to the next person.
HERMES_BACKEND_BOUND_ADDRESS = "127.0.0.1:9119"


def hermes_backend_ws_url(token: str) -> str:
    """The backend's JSON-RPC socket, as the backend itself is addressed.

    Not the address this host dials — that is the forwarder's, carried
    separately as the connection's ``dial`` — because Hermes refuses an
    upgrade whose `Host` names anything but its own loopback bind. The
    reasoning, the measurement behind it and the rejected alternatives are on
    :meth:`HermesBackendChannel.connect`.

    The credential is a QUERY parameter rather than a header, which is Hermes'
    own arrangement for a loopback bind and not a shortcut: an Authorization
    header on this upgrade is refused with 403, found by probing a real
    backend before any of this was written.
    """

    return (
        f"ws://{HERMES_BACKEND_BOUND_ADDRESS}"
        f"/api/ws?token={quote(str(token), safe='')}"
    )


def hermes_backend_dial(origin: str) -> tuple[str, int]:
    """Split a resolved endpoint origin into the ``(host, port)`` to connect to.

    The sandbox backend's endpoint face answers with whatever is reachable from
    here — a pod address on Kubernetes without an ingress, a mapped host port
    on local Docker — so both halves come from it rather than from a constant.
    """

    parsed = urlsplit(str(origin or "").strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise APIError(
            code="HERMES_GATEWAY_START_FAILED",
            message=f"Hermes backend endpoint is not an http origin: {origin!r}",
            status_code=502,
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (str(parsed.hostname), int(port))


def _hermes_backend_token(normalized: dict[str, Any]) -> str:
    """The WebSocket credential for this profile's in-box Hermes backend.

    A function of the profile, not a random draw, because the backend is a
    supervised service: supervisord may restart it at any time, and a secret
    drawn afresh on each start would leave the host holding one the restarted
    box rejects. Deriving it lets the host present the same credential for the
    life of the profile without storing a second secret anywhere.

    It authorizes a loopback socket inside one workspace box whose profile
    directory is already mode 700, so its blast radius is that box. It is not
    a platform credential and must never be reused as one.
    """

    material = "|".join(
        (
            "astrabox-hermes-backend",
            str(normalized.get("linux_user") or ""),
            str(normalized.get("home_dir") or ""),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _build_hermes_profile_env_exports(env: dict[str, str]) -> str:
    lines = [
        "# Generated by AstraBox. Mode 0600; used only by this Hermes profile.",
    ]
    for key, value in sorted(env.items()):
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            raise APIError(
                code="HERMES_PROFILE_ENV_INVALID",
                message=f"invalid Hermes profile env key: {key!r}",
                status_code=500,
            )
        lines.append(f"export {key}={shlex.quote(str(value))}")
    return "\n".join(lines) + "\n"


#: The supervised program `astrabox-hermes-serve` runs, named in
#: `containers/sandbox-hermes/supervisord.hermes.conf`.
_HERMES_BACKEND_PROGRAM = "astrabox-hermes"


async def _hermes_profile_env_standing(
    run_fn: Any, *, path: str, digest: str
) -> str:
    """Whether the box already holds this profile, a different one, or none.

    Asked before writing, because the answer decides whether the resident
    backend has to be restarted and the write destroys the evidence for it. The
    comparison happens inside the box so the profile — which carries the model
    credential — is never read back out of it.

    ``ABSENT`` is the first materialization: the backend has not started yet,
    `astrabox-hermes-serve` is still waiting for this very file, and restarting
    would only fight its own readiness gate. ``UNCHANGED`` is every ordinary
    wake. ``CHANGED`` is the one that costs a restart.
    """

    probe = (
        'set -euo pipefail; '
        'if [ ! -f "$1" ]; then echo ABSENT; '
        'elif [ "$(sha256sum < "$1" | cut -d" " -f1)" = "$2" ]; then echo UNCHANGED; '
        'else echo CHANGED; fi'
    )
    result = await run_fn(
        "bash -lc " + shlex.quote(probe) + " _ " + shlex.quote(path) + " " + shlex.quote(digest)
    )
    output = _hermes_command_output(result)
    for verdict in ("UNCHANGED", "CHANGED", "ABSENT"):
        if verdict in output:
            return verdict
    raise APIError(
        code="HERMES_PROFILE_SETUP_FAILED",
        message=(
            "could not read the standing Hermes profile to decide whether the "
            f"resident backend must restart; output={output[:2000]!r}"
        ),
        status_code=502,
    )


async def _restart_hermes_backend(run_fn: Any) -> None:
    """Restart the resident backend so it re-reads a profile that changed.

    Hermes reads its configuration once, when the agent is constructed at
    startup: the model route, the API key and everything
    `astrabox-hermes-config-merge` writes into `~/.hermes/config.json`. The PTY
    design got this for free — every conversation was a new process — and the
    resident one does not, so changing an Assistant's model would otherwise
    take effect only when the box next restarted, silently and with the old
    model answering in the meantime.

    Restarting drops the box's in-flight turns, which is why it is done only
    when the profile actually changed: that is a deliberate act by the owner of
    every session on this box, and the alternative is answering them from a
    configuration they replaced.

    The host is not assembling a capability here — the service is the image's,
    supervised by the image — it is telling a service its inputs moved. The
    attach that follows spends its bounded wait on the backend coming back.
    """

    result = await run_fn(
        "bash -lc "
        + shlex.quote("set -euo pipefail; exec supervisorctl restart " + _HERMES_BACKEND_PROGRAM)
    )
    error = getattr(result, "error", None)
    output = _hermes_command_output(result)
    if error or "started" not in output:
        raise APIError(
            # The registered code for "the resident gateway is not up under
            # the profile this host just wrote", which is exactly what a
            # refused restart leaves behind. A separate code would name the
            # same owner, the same phase and the same client action.
            code="HERMES_GATEWAY_START_FAILED",
            message=(
                "the Hermes backend did not restart for a changed profile, so it "
                f"would keep serving the old one: {error or 'no start marker'}; "
                f"output={output[:2000]!r}"
            ),
            status_code=502,
        )
    logger.info("restarted the resident Hermes backend for a changed profile")


async def _install_hermes_profile_env_file(
    sandbox: Any,
    *,
    path: str,
    content: str,
) -> None:
    await install_verified_text_script(
        sandbox,
        path=path,
        content=content,
        mode=0o600,
        error_code="HERMES_PROFILE_ENV_INSTALL_FAILED",
        error_message="failed to install Hermes profile env file",
    )


def _hermes_command_output(result: Any) -> str:
    chunks: list[str] = []
    for attr in ("stdout", "output"):
        value = getattr(result, attr, None)
        if value:
            chunks.append(str(value))
    logs = getattr(result, "logs", None)
    for stream_name in ("stdout", "stderr"):
        stream = getattr(logs, stream_name, None) if logs is not None else None
        for item in stream or []:
            text = getattr(item, "text", None)
            if text:
                chunks.append(str(text))
    return "".join(chunks)


def _hermes_credential_request(model_access: ResolvedModelAccess) -> ModelCredentialRequest:
    """Hermes's model access, declared once for create and for re-attach.

    Hermes speaks the OpenAI wire and refuses an Anthropic-shaped origin, so
    its base URL is resolved in the engine's own terms before the platform
    admits it.
    """

    return ModelCredentialRequest(
        access=replace(
            model_access,
            base_url=_resolve_hermes_openai_base_url(model_access),
        ),
        request_paths=("chat/completions",),
        missing_code="HERMES_MODEL_API_KEY_NOT_CONFIGURED",
        missing_message="model api key not configured for Hermes template",
        missing_status=500,
    )


def _build_hermes_sandbox_env() -> dict[str, str]:
    """Build sandbox-level env that is safe before a profile is selected.

    HERMES_HOME and model configuration are profile-owned and are written only
    after NAS and Linux identity provisioning. Hermes' TUI control path needs
    no listening port or application bearer token.
    """
    return {
        "ASTRABOX_HERMES_AUTOSTART": "false",
    }


def _build_hermes_config_overwrite(
    deployment_settings: Any,
    *,
    template: Any,
    model_access: ResolvedModelAccess,
    mcp_deployment_id: str | None = None,
) -> dict[str, Any]:
    """Platform-owned profile yaml (force-overwrite).

    Composes the model/custom_providers subtree. Consumed inside the runtime
    image by scripts/runtime/hermes_config_merge.py via the
    ``ASTRABOX_HERMES_CONFIG_OVERWRITE`` env JSON blob.
    """
    model_config_payload = dict(model_access.configuration)
    model_base_url = _resolve_hermes_openai_base_url(model_access)
    if not model_base_url:
        raise APIError(
            code="HERMES_MODEL_BASE_URL_NOT_CONFIGURED",
            message="OpenAI-compatible model base_url not configured for hermes template",
            status_code=500,
        )
    model_name = _resolve_hermes_model_name(model_access)
    if not model_name:
        raise APIError(
            code="HERMES_MODEL_NAME_NOT_CONFIGURED",
            message="model_name not configured for hermes template",
            status_code=500,
        )
    api_mode = (
        str(
            model_config_payload.get("api_mode")
            or model_config_payload.get("hermes_api_mode")
            or _HERMES_DEFAULT_API_MODE
        ).strip()
        or _HERMES_DEFAULT_API_MODE
    )
    provider_name = (
        str(
            model_config_payload.get("provider_name")
            or model_config_payload.get("custom_provider_name")
            or _HERMES_CUSTOM_PROVIDER_NAME
        ).strip()
        or _HERMES_CUSTOM_PROVIDER_NAME
    )
    provider_slug = (
        re.sub(r"[^a-z0-9_-]+", "-", provider_name.lower()).strip("-") or "astrabox"
    )
    payload: dict[str, Any] = {
        "model": {
            "default": model_name,
            "provider": f"custom:{provider_slug}",
        },
        "custom_providers": [
            {
                "name": provider_name,
                "base_url": model_base_url,
                "key_env": _HERMES_MODEL_API_KEY_ENV,
                "model": model_name,
                "api_mode": api_mode,
            }
        ],
    }
    if template_mcp_servers(getattr(template, "mcp_servers", None)):
        mcp_servers = runtime_mcp_servers_for_binding(
            getattr(template, "mcp_servers", None),
            proxy_base_url=str(
                getattr(deployment_settings, "mcp_proxy_base_url", "") or ""
            ),
            deployment_id=str(mcp_deployment_id or "").strip(),
        )
        if mcp_servers:
            payload["mcp_servers"] = mcp_servers
    return payload


def _build_hermes_config_defaults(
    *, plugin_config_defaults: dict[str, Any] | None = None
) -> dict[str, Any]:
    """User-overridable profile yaml (setdefault).

    The platform base (terminal/approvals/security) plus whatever the configured
    plugins contribute (e.g. a memory provider's ``memory: {provider: …}``).
    Anything the user later sets via ``hermes config set …`` is preserved.
    """
    defaults: dict[str, Any] = {
        "terminal": {"backend": "local"},
        "approvals": {"mode": "off", "cron_mode": "approve"},
        "security": {"tirith_enabled": False},
    }
    if plugin_config_defaults:
        defaults = _deep_merge(defaults, plugin_config_defaults)
    return defaults


def _build_hermes_skill_source_dirs(
    deployment_settings: Any,
    *,
    model_config_payload: dict[str, Any],
    repo_source_dirs: list[str] | None = None,
) -> list[str]:
    """Hermes-native platform skill distribution source directories.

    Claude Code template ``skills`` are intentionally not used here: Hermes
    discovers skills from ``$HERMES_HOME/skills``. The runtime bootstrap copies
    missing platform skills from these shared source dirs into the user's
    profile, preserving any existing user-edited skill copy.
    """
    values: list[str] = []
    values.extend(
        _normalize_hermes_skill_source_dirs(
            getattr(deployment_settings, "hermes_skill_source_dirs", [])
        )
    )
    hermes_config = model_config_payload.get("hermes")
    if isinstance(hermes_config, dict):
        skills_config = hermes_config.get("skills")
        if isinstance(skills_config, dict):
            values.extend(
                _normalize_hermes_skill_source_dirs(skills_config.get("source_dirs"))
            )
    values.extend(
        _normalize_hermes_skill_source_dirs(
            model_config_payload.get("hermes_skill_source_dirs")
        )
    )
    values.extend(_normalize_hermes_skill_source_dirs(repo_source_dirs))

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _build_hermes_soul_content(model_config_payload: dict[str, Any]) -> str:
    hermes_config = model_config_payload.get("hermes")
    if not isinstance(hermes_config, dict):
        return ""
    raw_soul = hermes_config.get("soul")
    if raw_soul is None:
        return ""
    if not isinstance(raw_soul, str):
        raise APIError(
            code="HERMES_SOUL_INVALID",
            message="model_config.hermes.soul must be a string",
            status_code=500,
        )
    content = raw_soul.strip()
    if not content:
        return ""
    return f"{content}\n"


def _build_hermes_cron_jobs_payload(model_config_payload: dict[str, Any]) -> list[dict[str, Any]]:
    hermes_config = model_config_payload.get("hermes")
    if hermes_config is None:
        return []
    if not isinstance(hermes_config, dict):
        raise APIError(
            code="HERMES_CRON_INVALID",
            message="model_config.hermes must be an object when configuring Hermes cron",
            status_code=500,
        )
    cron_config = hermes_config.get("cron")
    if cron_config is None:
        return []
    if not isinstance(cron_config, dict):
        raise APIError(
            code="HERMES_CRON_INVALID",
            message="model_config.hermes.cron must be an object",
            status_code=500,
        )
    jobs = cron_config.get("jobs", [])
    if not isinstance(jobs, list):
        raise APIError(
            code="HERMES_CRON_INVALID",
            message="model_config.hermes.cron.jobs must be an array",
            status_code=500,
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_job in enumerate(jobs):
        label = f"model_config.hermes.cron.jobs[{index}]"
        if not isinstance(raw_job, dict):
            raise APIError(
                code="HERMES_CRON_INVALID",
                message=f"{label} must be an object",
                status_code=500,
            )
        job_id = str(raw_job.get("id") or "").strip()
        if not job_id:
            raise APIError(
                code="HERMES_CRON_INVALID",
                message=f"{label}.id is required",
                status_code=500,
            )
        if job_id in seen:
            raise APIError(
                code="HERMES_CRON_INVALID",
                message=f"{label}.id duplicates {job_id!r}",
                status_code=500,
            )
        seen.add(job_id)
        schedule = raw_job.get("schedule")
        if not isinstance(schedule, (str, dict)):
            raise APIError(
                code="HERMES_CRON_INVALID",
                message=f"{label}.schedule must be a string or object",
                status_code=500,
            )
        no_agent = raw_job.get("no_agent", False)
        if not isinstance(no_agent, bool):
            raise APIError(
                code="HERMES_CRON_INVALID",
                message=f"{label}.no_agent must be a boolean",
                status_code=500,
            )
        prompt = raw_job.get("prompt")
        script = raw_job.get("script")
        if no_agent:
            if not isinstance(script, str) or not script.strip():
                raise APIError(
                    code="HERMES_CRON_INVALID",
                    message=f"{label}.script is required when no_agent=true",
                    status_code=500,
                )
        elif not isinstance(prompt, str) or not prompt.strip():
            raise APIError(
                code="HERMES_CRON_INVALID",
                message=f"{label}.prompt is required",
                status_code=500,
            )
        normalized.append(dict(raw_job))
    return normalized


def _normalize_hermes_skill_source_dirs(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list) or isinstance(raw, tuple):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [str(raw).strip()] if str(raw).strip() else []


async def _prepare_hermes_skill_repos(
    sandbox: Any,
    *,
    deployment_settings: Any,
    model_config_payload: dict[str, Any],
    platform_secrets: dict[str, str],
) -> list[str]:
    repos = _get_hermes_skill_repos(deployment_settings, model_config_payload)
    if not repos:
        return []
    command_runner = getattr(sandbox, "commands", None)
    run_fn = getattr(command_runner, "run", None) if command_runner is not None else None
    if not callable(run_fn):
        raise APIError(
            code="HERMES_SKILL_REPO_CLONE_FAILED",
            message="sandbox command runner is required to clone hermes skill repos",
            status_code=502,
        )

    cache_hash = _hermes_skill_repo_cache_hash(repos)
    cache_dir = f"{HERMES_SKILL_REPO_CACHE_DIR.rstrip('/')}/{cache_hash}"
    payload_repos: list[dict[str, Any]] = []
    for index, repo in enumerate(repos):
        label = f"hermes_skill_repos[{index}]"
        secret_name = str(repo.get("deploy_key_secret_name") or "").strip()
        if not secret_name:
            raise APIError(
                code="HERMES_SKILL_REPO_MISSING_KEY",
                message=f"{label}.deploy_key_secret_name is required for ssh protocol",
                status_code=500,
            )
        private_key = platform_secrets.get(secret_name)
        if not private_key:
            raise APIError(
                code="HERMES_SKILL_REPO_MISSING_KEY",
                message=(
                    f"the platform supplied no deploy key for {label} "
                    f"(secret_name={secret_name!r})"
                ),
                status_code=500,
            )
        private_key = _normalize_deploy_private_key(private_key, secret_name=secret_name)
        payload_repos.append(
            {
                "index": index,
                "url": repo["url"],
                "branch": repo.get("branch") or "",
                "depth": repo.get("depth"),
                "sha": repo.get("sha") or "",
                "skill_paths": repo.get("skill_paths") or ["."],
                "checkout_rel": _hermes_skill_repo_checkout_rel(index, repo),
                "key_b64": base64.b64encode(private_key.encode("utf-8")).decode("ascii"),
            }
        )

    payload = {
        "cache_dir": cache_dir,
        "cache_hash": cache_hash,
        "repos": payload_repos,
    }
    payload_b64 = base64.b64encode(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    result = await run_fn(
        f"python3 {shlex.quote(_HERMES_SKILL_REPO_CACHE_SCRIPT_PATH)} {shlex.quote(payload_b64)}"
    )
    output = _hermes_command_output(result)
    if getattr(result, "error", None) or "HERMES_SKILL_REPO_CACHE_READY" not in output:
        raise APIError(
            code="HERMES_SKILL_REPO_CLONE_FAILED",
            message=(
                "failed to prepare hermes skill repo cache: "
                f"error={getattr(result, 'error', None) or 'missing readiness marker'}; "
                f"output={output[:2000]!r}"
            ),
            status_code=502,
        )
    return _hermes_skill_repo_source_dirs(cache_dir, repos)


def _get_hermes_skill_repos(
    deployment_settings: Any,
    model_config_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_repos: list[Any] = []
    raw_repos.extend(getattr(deployment_settings, "hermes_skill_repos", []) or [])
    hermes_config = model_config_payload.get("hermes")
    if isinstance(hermes_config, dict):
        skills_config = hermes_config.get("skills")
        if isinstance(skills_config, dict):
            raw_repos.extend(skills_config.get("repos") or [])
    raw_repos.extend(model_config_payload.get("hermes_skill_repos") or [])
    return _normalize_hermes_skill_repos(raw_repos)


def _normalize_hermes_skill_repos(raw: Any) -> list[dict[str, Any]]:
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise APIError(
            code="HERMES_SKILL_REPO_INVALID",
            message=f"hermes skill repos must be a list, got {type(raw).__name__}",
            status_code=500,
        )
    repos: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise APIError(
                code="HERMES_SKILL_REPO_INVALID",
                message=f"hermes skill repos[{index}] must be an object",
                status_code=500,
            )
        url = str(item.get("url") or "").strip()
        if not url:
            raise APIError(
                code="HERMES_SKILL_REPO_INVALID",
                message=f"hermes skill repos[{index}].url is required",
                status_code=500,
            )
        protocol = str(item.get("protocol") or "ssh").strip().lower()
        if protocol != "ssh":
            raise APIError(
                code="HERMES_SKILL_REPO_UNSUPPORTED_PROTOCOL",
                message=f"hermes skill repos[{index}].protocol={protocol!r} not supported",
                status_code=500,
            )
        if not url.startswith("git@"):
            raise APIError(
                code="HERMES_SKILL_REPO_INVALID",
                message=(
                    f"hermes skill repos[{index}].url={url!r} must be an SSH URL "
                    "(git@host:group/repo.git)"
                ),
                status_code=500,
            )
        repos.append(
            {
                "url": url,
                "protocol": protocol,
                "deploy_key_secret_name": str(item.get("deploy_key_secret_name") or "").strip(),
                "branch": str(item.get("branch") or item.get("ref") or "").strip(),
                "depth": item.get("depth"),
                "sha": str(item.get("sha") or item.get("commit") or "").strip(),
                "skill_paths": _normalize_hermes_skill_paths(item.get("skill_paths"), index),
            }
        )
    return repos


def _normalize_hermes_skill_paths(raw: Any, repo_index: int) -> list[str]:
    if raw is None or raw == "":
        return ["."]
    values = raw if isinstance(raw, list) else [raw]
    paths: list[str] = []
    for path_index, value in enumerate(values):
        path = str(value or "").strip()
        if not path:
            raise APIError(
                code="HERMES_SKILL_REPO_INVALID",
                message=f"hermes skill repos[{repo_index}].skill_paths[{path_index}] is empty",
                status_code=500,
            )
        if path.startswith("/"):
            raise APIError(
                code="HERMES_SKILL_REPO_INVALID",
                message=f"hermes skill repos[{repo_index}].skill_paths[{path_index}] must be relative",
                status_code=500,
            )
        normalized = posixpath.normpath(path)
        if normalized in {"", "."}:
            normalized = "."
        if normalized == ".." or normalized.startswith("../"):
            raise APIError(
                code="HERMES_SKILL_REPO_INVALID",
                message=f"hermes skill repos[{repo_index}].skill_paths[{path_index}] escapes repo root",
                status_code=500,
            )
        paths.append(normalized)
    return paths


def _hermes_skill_repo_cache_hash(repos: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"hermes_skill_repos": repos},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hermes_skill_repo_checkout_rel(index: int, repo: dict[str, Any]) -> str:
    return f"repos/{index:02d}-{_repo_slug(str(repo.get('url') or 'repo'))}"


def _hermes_skill_repo_source_dirs(cache_dir: str, repos: list[dict[str, Any]]) -> list[str]:
    dirs: list[str] = []
    root = str(cache_dir).rstrip("/")
    for index, repo in enumerate(repos):
        checkout = f"{root}/{_hermes_skill_repo_checkout_rel(index, repo)}"
        for skill_path in repo.get("skill_paths") or ["."]:
            dirs.append(checkout if skill_path == "." else f"{checkout}/{skill_path}")
    return dirs


def _repo_slug(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", tail.strip()).strip(".-")
    return cleaned or "repo"


@dataclass(frozen=True)
class _HermesPlugins:
    """Aggregated, runtime-substituted contribution of the configured plugins."""

    env: dict[str, str]
    env_keys: tuple[str, ...]
    config_files: dict[str, Any]
    config_defaults: dict[str, Any]


def _substitute_runtime_vars(value: Any, runtime_vars: dict[str, str]) -> Any:
    """Recursively substitute ``{name}`` runtime placeholders in string leaves.

    Only the whitelisted names in ``runtime_vars`` (conversation_user_id,
    assistant_id, user_id) are replaced; an unknown ``{...}`` is left verbatim so
    a plugin's own literal braces survive.
    """
    if isinstance(value, str):
        out = value
        for name, replacement in runtime_vars.items():
            out = out.replace("{" + name + "}", replacement)
        return out
    if isinstance(value, dict):
        return {k: _substitute_runtime_vars(v, runtime_vars) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_runtime_vars(v, runtime_vars) for v in value]
    return value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base`` (overlay wins at leaves)."""
    result = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _build_hermes_plugins(
    model_config_payload: dict[str, Any],
    *,
    runtime_vars: dict[str, str],
) -> _HermesPlugins:
    """Aggregate the operator-configured Hermes plugins into engine-neutral parts.

    Plugins are pure configuration under ``model_config.hermes.plugins`` — the engine
    core knows no plugin by name. Each entry is a dict::

        {
          "name": "openviking",              # label only, for diagnostics
          "env": {"OPENVIKING_ENDPOINT": "https://…",
                  "OPENVIKING_USER": "{conversation_user_id}"},
          "config_files": {".openviking/ov.conf": { … }},  # written under profile home
          "config": {"memory": {"provider": "openviking"}} # merged into config defaults
        }

    ``{conversation_user_id}`` / ``{assistant_id}`` / ``{user_id}`` placeholders in
    any string leaf of ``env`` / ``config_files`` / ``config`` are substituted
    from ``runtime_vars``. A memory provider such as OpenViking is expressed
    entirely as one such entry in the template — nothing here is provider-specific.
    """
    hermes_config = model_config_payload.get("hermes")
    hermes_config = hermes_config if isinstance(hermes_config, dict) else {}
    raw_plugins = hermes_config.get("plugins")
    if not isinstance(raw_plugins, list):
        return _HermesPlugins(env={}, env_keys=(), config_files={}, config_defaults={})

    env: dict[str, str] = {}
    env_keys: list[str] = []
    config_files: dict[str, Any] = {}
    config_defaults: dict[str, Any] = {}
    for entry in raw_plugins:
        if not isinstance(entry, dict):
            raise APIError(
                code="HERMES_PLUGIN_INVALID",
                message="each hermes plugin must be an object",
                status_code=400,
            )
        plugin_env = _substitute_runtime_vars(entry.get("env") or {}, runtime_vars)
        if not isinstance(plugin_env, dict):
            raise APIError(code="HERMES_PLUGIN_INVALID", message="plugin.env must be an object", status_code=400)
        for key, value in plugin_env.items():
            env[str(key)] = str(value)
            env_keys.append(str(key))
        plugin_files = _substitute_runtime_vars(entry.get("config_files") or {}, runtime_vars)
        if not isinstance(plugin_files, dict):
            raise APIError(code="HERMES_PLUGIN_INVALID", message="plugin.config_files must be an object", status_code=400)
        config_files.update(plugin_files)
        plugin_config = _substitute_runtime_vars(entry.get("config") or {}, runtime_vars)
        if not isinstance(plugin_config, dict):
            raise APIError(code="HERMES_PLUGIN_INVALID", message="plugin.config must be an object", status_code=400)
        config_defaults = _deep_merge(config_defaults, plugin_config)

    return _HermesPlugins(
        env=env,
        env_keys=tuple(dict.fromkeys(env_keys)),
        config_files=config_files,
        config_defaults=config_defaults,
    )


def _resolve_hermes_openai_base_url(
    model_access: ResolvedModelAccess,
) -> str:
    model_config_payload = model_access.configuration
    explicit_openai_url = str(
        model_config_payload.get("openai_base_url")
        or model_config_payload.get("openai_compatible_base_url")
        or ""
    ).strip()
    if explicit_openai_url:
        base_url = _normalize_hermes_openai_base_url(explicit_openai_url)
    else:
        base_url = _normalize_hermes_openai_base_url(model_access.base_url or "")
    if _looks_anthropic_compatible_base_url(base_url):
        raise APIError(
            code="HERMES_MODEL_BASE_URL_NOT_OPENAI_COMPATIBLE",
            message=(
                "hermes template requires OpenAI-compatible model base_url; "
                "configure template.model_config.base_url or openai_base_url"
            ),
            status_code=500,
        )
    return base_url


def _normalize_hermes_openai_base_url(raw_value: str) -> str:
    value = str(raw_value or "").strip().rstrip("/")
    if not value:
        return ""
    lowered = value.lower()
    for suffix in ("/chat/completions", "/responses"):
        if lowered.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    return value


def _looks_anthropic_compatible_base_url(value: str) -> bool:
    lowered = str(value or "").strip().lower().rstrip("/")
    return lowered.endswith("/api/anthropic") or "/api/anthropic/" in lowered


def _resolve_hermes_model_name(
    model_access: ResolvedModelAccess,
) -> str:
    model_config_payload = model_access.configuration
    for key in ("hermes_model_name", "openai_model_name"):
        value = str(model_config_payload.get(key) or "").strip()
        if value:
            return value
    return str(model_access.model_name or "").strip()


class HermesProcessDisposal:
    """Dispose one Hermes vendor session from its durable engine anchor."""

    async def dispose_process(
        self,
        *,
        sandbox_id: str,
        engine_turn_id: str,
    ) -> None:
        """Close one conversation's session on the profile's resident gateway.

        The journal retains the opaque turn id after the active snapshot has
        settled. Decoding it belongs to this adapter; the runtime manager does
        not need to understand Hermes' identifiers.

        Only the conversation's vendor session ends here. The backend is the
        workspace's resident runtime, shared with sibling conversations and
        owned by the image's supervisor, so nothing here stops it.
        """
        anchor = decode_turn_anchor(engine_turn_id)
        resident = gateway_handle_for_sandbox(sandbox_id)
        if resident is None:
            # Only this host's own attachment can carry the close. A PTY-era
            # disposal could open a temporary one, because the anchor named
            # the PTY and execd would hand it over; the backend's address now
            # needs the profile's credential, which an opaque turn id does not
            # carry and must not.
            #
            # So the vendor session is left open, and it is bounded rather
            # than leaked: it lives inside a backend that is restarted with
            # its box and destroyed with the workspace. Said out loud because
            # it IS a reduction from what the PTY path could do.
            logger.info(
                "Hermes session.close skipped: this host holds no attachment "
                "to sandbox=%s; the vendor session ends with the backend "
                "(tui_session=%s)",
                sandbox_id,
                anchor["tui_session_id"],
            )
            return
        try:
            await resident.request(
                "session.close",
                {"session_id": anchor["tui_session_id"]},
            )
        except Exception as exc:
            logger.warning(
                "Hermes session.close failed on the resident backend: "
                "sandbox=%s tui_session=%s err=%s",
                sandbox_id,
                anchor["tui_session_id"],
                exc,
            )

_HERMES_PROCESS_DISPOSAL = HermesProcessDisposal()


# Self-register on import. Idempotent — registry guards against duplicates.
register_engine_adapter("assistant", HermesEngineAdapter())
