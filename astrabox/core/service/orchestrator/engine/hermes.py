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
from urllib.parse import urlsplit
import re
import shlex
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

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
    HermesGatewayHandle,
    gateway_handle_for_sandbox,
    resolve_gateway_handle,
    wait_until_backend_idle,
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


HERMES_RUNTIME_IMAGE_COMPONENT = "sandbox-hermes"

logger = get_logger(__name__)

_HERMES_PROFILE_SETUP_SCRIPT_PATH = "/usr/local/bin/astrabox-hermes-profile-setup"
_HERMES_PROFILE_ENV_FILENAME = "astrabox-hermes.env"
_HERMES_RUNTIME_STATE_FILENAME = "astrabox-runtime-state.json"
_HERMES_STATE_BOOTSTRAP = "/opt/astrabox/hermes/hermes_state_bootstrap.py"
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

# Profile envs consumed by scripts/runtime/hermes_config_merge.py inside the
# runtime image, which materializes the profile-owned config.yaml and SOUL.md.
# The backend's launcher runs it before Hermes starts, and profile preparation
# runs it again in place when only these inputs changed.
_HERMES_CONFIG_OVERWRITE_ENV = "ASTRABOX_HERMES_CONFIG_OVERWRITE"
_HERMES_CONFIG_DEFAULTS_ENV = "ASTRABOX_HERMES_CONFIG_DEFAULTS"
# Present exactly when the Assistant sets a system prompt. Hermes reads
# ``$HERMES_HOME/SOUL.md`` as its identity, the first slot of its system prompt,
# replacing its built-in "You are Hermes Agent" text and keeping the rest of its
# prompt; absent, the merge hands SOUL.md back to Hermes' own default.
_HERMES_SOUL_B64_ENV = "ASTRABOX_HERMES_SOUL_B64"
# What the merge prints last when it finished: it wrote config.yaml, or had
# nothing to write.
_HERMES_CONFIG_MERGE_MARKERS = ("ASTRABOX_HERMES_CONFIG_READY", "ASTRABOX_HERMES_CONFIG_NOOP")

# What a running Hermes backend reads again for every new conversation, and so
# never needs a restart to apply. Hermes builds each session's agent when the
# session is created: the model and its provider come from config.yaml, which
# it re-reads whenever the file changes (``_load_cfg`` and ``load_config`` are
# keyed on the file's mtime), and the system prompt is assembled then, reading
# SOUL.md from disk. A live session keeps its system prompt and adopts a
# changed model at its next turn start (``_sync_agent_model_with_config``);
# neither touches a turn in progress.
_HERMES_PER_SESSION_ENVS = frozenset(
    {_HERMES_CONFIG_OVERWRITE_ENV, _HERMES_CONFIG_DEFAULTS_ENV, _HERMES_SOUL_B64_ENV}
)
# The part of config.yaml Hermes reads only when the process starts: MCP
# servers are discovered once into a process-wide registry, and a session's
# agent snapshots its tools from it. The vendor's live alternative,
# ``reload.mcp``, tears that registry down under every session and fails
# their in-flight MCP calls, so it is no gentler than a restart.
_HERMES_PROCESS_CONFIG_KEYS = ("mcp_servers",)

#: Beside the profile env file: the digest of the process configuration the
#: running backend was started from (:func:`_hermes_process_digest`).
_HERMES_PROCESS_DIGEST_FILENAME = "astrabox-hermes-process.sha256"


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
                    "/usr/local/bin/astrabox-provision-assistant-profile",
                    "/usr/local/bin/astrabox-hermes-profile-setup",
                ),
            ),
            conversation_placement=CONVERSATION_PLACEMENT_PER_ACCOUNT,
            default_runtime_image=release_image(HERMES_RUNTIME_IMAGE_COMPONENT),
            # Hermes renders platform MCP bindings into its native config.
            # It discovers skills in its own profile (``$HERMES_HOME/skills``),
            # which the platform does not populate, and it has no Claude
            # plugin format.
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
        """Declare the runtime-state target Hermes' profile restore reads."""

        _ = template, model_access, deployment_settings
        return EngineStartupMaterialRequest(secret_names=(), runtime_state_store=True)

    async def activate_runtime(
        self,
        context: EngineStartupContext,
    ) -> SessionRuntime:
        """Prepare Hermes's native profile in an already assigned box."""

        template = context.template
        workspace_plan = context.workspace_plan
        user_id = context.user_id
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

                async def _wait_until_backend_idle() -> None:
                    await wait_until_backend_idle(
                        await self._resident_backend(
                            sandbox,
                            identity=runtime_identity,
                            profile_ref=profile_ref,
                            spawn_fingerprint=None,
                        )
                    )

                spawn_fingerprint = await _prepare_hermes_profile(
                    sandbox,
                    identity=runtime_identity,
                    deployment_settings=context.deployment_settings,
                    template=template,
                    model_access=context.model_access,
                    model_api_key=context.model_credential,
                    runtime_env=dict(context.runtime_env or {}),
                    mcp_deployment_id=context.platform_mcp_deployment_id,
                    runtime_state_store=context.runtime_state_store,
                    wait_until_backend_idle=_wait_until_backend_idle,
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

        Every conversation of the profile finds the resident backend and pays
        only ``session.create``/``session.resume``. ``spawn_fingerprint`` is
        the process configuration :func:`_prepare_hermes_profile` left the
        backend running under; see :meth:`_resident_backend`.
        """

        gateway = await self._resident_backend(
            sandbox,
            identity=identity,
            profile_ref=profile_ref,
            spawn_fingerprint=spawn_fingerprint,
        )
        return HermesEngineClient(
            gateway=gateway,
            platform_session_id=session_id,
            resume_session_key=resume_session_key,
        )

    async def _resident_backend(
        self,
        sandbox: Any,
        *,
        identity: dict[str, Any] | None,
        profile_ref: dict[str, str] | None,
        spawn_fingerprint: str | None,
    ) -> HermesGatewayHandle:
        """This host's attachment to the box's resident Hermes backend.

        ``spawn_fingerprint`` names the process configuration the backend runs
        under (:func:`_hermes_process_digest`). An attachment established
        under a different one belongs to a backend that has since restarted,
        so it is dropped rather than kept for new sessions. A change Hermes
        reads per session leaves the fingerprint alone, and with it the
        attachment every other conversation of the profile is streaming on.
        ``None`` accepts the standing attachment, for callers that do not
        touch the profile.
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
        return await resolve_gateway_handle(
            url=hermes_backend_ws_url(endpoint.origin),
            headers=hermes_backend_headers(
                endpoint.headers, _hermes_backend_token(normalized)
            ),
            sandbox_id=sandbox_id,
            profile_key=profile_ref["profile_key"],
            spawn_fingerprint=spawn_fingerprint,
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
    runtime_state_store: dict[str, Any] | None,
    runtime_env: dict[str, str] | None = None,
    mcp_deployment_id: str | None = None,
    wait_until_backend_idle: Callable[[], Awaitable[None]],
) -> str:
    """Materialize the profile in the box; return its process fingerprint.

    This is where a changed Assistant takes effect for the conversation being
    started, and how depends on what changed. Hermes reads the model, its
    provider and SOUL.md for each new session (``_HERMES_PER_SESSION_ENVS``),
    so those are written into the profile in place and the running backend
    keeps serving everyone else. What Hermes reads only when its process
    starts — its environment and its MCP servers — needs a restart, and a
    restart ends every turn running in the backend. So that path first waits,
    through ``wait_until_backend_idle``, until nothing is running, while this
    conversation stays in preparation.

    The returned fingerprint is :func:`_hermes_process_digest`: an attachment
    to a backend started under a different one is dropped downstream.
    """

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

    overwrite_blob = _build_hermes_config_overwrite(
        deployment_settings,
        template=template,
        model_access=model_access,
        mcp_deployment_id=mcp_deployment_id,
    )
    defaults_blob = _build_hermes_config_defaults()
    soul_content = _build_hermes_soul_content(template)
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
        "HERMES_HOME": normalized["config_dir"],
        **dict(runtime_env or {}),
    }
    if soul_content:
        env[_HERMES_SOUL_B64_ENV] = base64.b64encode(
            soul_content.encode("utf-8")
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
    process_digest = _hermes_process_digest(env, overwrite_blob)
    process_digest_path = posixpath.join(
        normalized["config_dir"], _HERMES_PROCESS_DIGEST_FILENAME
    )
    standing = await _hermes_process_standing(
        run_fn,
        env_path=profile_env_path,
        digest_path=process_digest_path,
        digest=process_digest,
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
        runtime_identity=normalized,
        mode=0o600,
        error_code="HERMES_PROFILE_ENV_INSTALL_FAILED",
        error_message="failed to install Hermes runtime-state target",
    )
    await _install_hermes_profile_env_file(
        sandbox,
        path=profile_env_path,
        content=profile_env_content,
        runtime_identity=normalized,
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
    # A freshly initialized box has not started Hermes yet: publishing just
    # released `astrabox-hermes-serve`, which merges this profile and then
    # starts the backend under it.
    running = "fresh=0" in _hermes_command_output(initialized)
    if running and standing == "PROCESS_UNCHANGED":
        # The recorded digest already names this process configuration.
        await _apply_hermes_profile_in_place(
            run_fn, normalized=normalized, profile_env_path=profile_env_path
        )
        return process_digest
    if running and standing == "PROCESS_CHANGED":
        await wait_until_backend_idle()
        await _restart_hermes_backend(run_fn)
    await install_verified_text_script(
        sandbox,
        path=process_digest_path,
        content=process_digest,
        runtime_identity=normalized,
        mode=0o600,
        error_code="HERMES_PROFILE_ENV_INSTALL_FAILED",
        error_message="failed to record the Hermes backend's process configuration",
    )
    return process_digest


def _hermes_process_digest(env: dict[str, str], overwrite_blob: dict[str, Any]) -> str:
    """The configuration a Hermes backend process fixes when it starts.

    Its environment (the profile env file minus the inputs Hermes reads per
    session) and the ``config.yaml`` keys it reads only at start. Two profiles
    with equal digests can be served by the same running backend; a different
    digest needs a restart.
    """

    payload = {
        "environment": {
            key: value for key, value in env.items() if key not in _HERMES_PER_SESSION_ENVS
        },
        "config": {key: overwrite_blob.get(key) for key in _HERMES_PROCESS_CONFIG_KEYS},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


#: The port `astrabox-hermes-forward` publishes the backend on. The backend
#: itself binds loopback (see `astrabox-hermes-serve` for why); this is the
#: one the sandbox's endpoint face knows about.
HERMES_BACKEND_PORT = 9118


#: The upgrade header that carries the backend credential from this host into
#: the box. `containers/sandbox-hermes/hermes_host_relay.py` moves it into the
#: `?token=` query Hermes reads, and names it in its own constant; the two must
#: match.
HERMES_BACKEND_TOKEN_HEADER = "X-AstraBox-Hermes-Token"


def hermes_backend_ws_url(origin: str) -> str:
    """The backend's JSON-RPC socket at the resolved endpoint ``origin``.

    The socket is addressed exactly as the sandbox backend returned the
    endpoint: a Pod address, an OpenSandbox ingress route, or execd's
    ``/proxy/<port>`` route on Docker. Whatever routes the connection reads
    that address — an ingress gateway routes on the path in uri mode and on
    `Host` in wildcard mode — so the scheme, authority and path all stay as
    issued. Hermes requires a `Host` naming its own loopback bind;
    `containers/sandbox-hermes/hermes_host_relay.py` supplies it inside the
    box, behind every one of those routes.

    The URL carries no credential; :func:`hermes_backend_headers` does.
    """

    parsed = urlsplit(str(origin or "").strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise APIError(
            code="HERMES_GATEWAY_START_FAILED",
            message=f"Hermes backend endpoint is not an http origin: {origin!r}",
            status_code=502,
        )
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path}/api/ws"


def hermes_backend_headers(endpoint_headers: Mapping[str, str], token: str) -> dict[str, str]:
    """The upgrade headers: the endpoint's own, plus the backend credential.

    On a loopback bind Hermes accepts the credential only as a `?token=`
    query parameter (`_ws_auth_reason` in `hermes_cli/web_server.py`), and
    every proxy between this host and the box records the request line: the
    OpenSandbox ingress gateway and execd's ``/proxy/<port>`` route both log
    the request URI. The credential therefore travels in
    :data:`HERMES_BACKEND_TOKEN_HEADER`, which those proxies forward without
    logging, and the relay in the box moves it into the query on the loopback
    hop. `Authorization` and `Cookie` are not used because the lifecycle
    server's relay removes them.
    """

    headers = {
        name: value
        for name, value in endpoint_headers.items()
        if name.lower() != HERMES_BACKEND_TOKEN_HEADER.lower()
    }
    headers[HERMES_BACKEND_TOKEN_HEADER] = str(token)
    return headers


def _hermes_backend_token(normalized: dict[str, Any]) -> str:
    """The WebSocket credential for this profile's in-box Hermes backend.

    Keyed by the deployment's own secret, through the same master-key
    derivation the platform uses elsewhere (:func:`derive_platform_key`), so it
    cannot be recomputed by anyone who merely knows the box's identity. A hash
    of that identity alone would not do: it is reproducible by a co-tenant, and
    on Kubernetes a sandbox Pod has no ingress NetworkPolicy, so any pod in the
    cluster can reach this port.

    Still a pure function of the profile, not a random draw, so a backend
    supervisord restarts keeps the credential the host already holds. Both the
    write into the box's profile env and the host's own upgrade compute it here,
    in the server process, from the same identity and the same secret, so they
    always agree; nothing is stored a second time. The subject is the box's
    account and home, which are what the token authorizes a loopback socket in.
    """
    from astrabox.core.service.orchestrator.platform_secret import (
        derive_platform_key,
        platform_secret_root,
    )

    subject = json.dumps(
        [
            str(normalized.get("linux_user") or ""),
            str(normalized.get("home_dir") or ""),
        ],
        separators=(",", ":"),
    )
    return derive_platform_key(
        platform_secret_root(),
        domain="astrabox-hermes-backend-ws",
        subject=subject,
    ).hex()


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


async def _hermes_process_standing(
    run_fn: Any, *, env_path: str, digest_path: str, digest: str
) -> str:
    """Whether the box's backend can serve this profile without a restart.

    ``ABSENT`` is the first materialization: no profile yet, so the backend has
    not started and `astrabox-hermes-serve` is waiting for this very file.
    ``PROCESS_UNCHANGED`` means the running backend was started under the same
    process configuration (the digest recorded beside the profile), so every
    other change is one Hermes reads per session. ``PROCESS_CHANGED`` — a
    different digest, or none recorded — needs a restart.

    Asked before writing, because the write destroys the evidence. The digest
    file holds a hash, never the profile, which carries the model credential.
    """

    probe = (
        'set -euo pipefail; '
        'if [ ! -f "$1" ]; then echo ABSENT; '
        'elif [ -f "$2" ] && [ "$(cat "$2")" = "$3" ]; then echo PROCESS_UNCHANGED; '
        'else echo PROCESS_CHANGED; fi'
    )
    result = await run_fn(
        "bash -lc " + shlex.quote(probe) + " _ " + shlex.quote(env_path)
        + " " + shlex.quote(digest_path) + " " + shlex.quote(digest)
    )
    output = _hermes_command_output(result)
    for verdict in ("PROCESS_UNCHANGED", "PROCESS_CHANGED", "ABSENT"):
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


async def _apply_hermes_profile_in_place(
    run_fn: Any, *, normalized: dict[str, Any], profile_env_path: str
) -> None:
    """Write the per-session inputs into the running backend's profile.

    The same merge `astrabox-hermes-serve` runs before starting Hermes, run as
    the profile's account against the profile just written. It rewrites
    ``config.yaml`` and SOUL.md atomically, so a session being built
    concurrently reads either the old files or the new ones, never a torn
    one; the next session built reads the new ones.
    """

    merge_script = (
        'set -euo pipefail; source "$1"; '
        'exec "$HERMES_VENV/bin/python" "$2"'
    )
    command = shlex.join([
        "runuser", "-u", normalized["linux_user"], "--", "env",
        f"HOME={normalized['home_dir']}",
        f"USER={normalized['linux_user']}",
        f"LOGNAME={normalized['linux_user']}",
        "bash", "--noprofile", "--norc", "-c", merge_script,
        "astrabox-hermes-config-merge", profile_env_path,
        _HERMES_CONFIG_MERGE_SCRIPT_PATH,
    ])
    result = await run_fn(command)
    # execd delivers each printed line as its own stdout event and the text
    # joins them with no separator, so the config marker follows the SOUL
    # marker on the same "line"; it is matched as a substring, like every
    # other marker this module reads.
    output = _hermes_command_output(result)
    if (
        getattr(result, "error", None)
        or getattr(result, "exit_code", 0) not in (0, None)
        or not any(marker in output for marker in _HERMES_CONFIG_MERGE_MARKERS)
    ):
        raise APIError(
            code="HERMES_PROFILE_SETUP_FAILED",
            message=(
                "failed to apply the changed Hermes profile to the running "
                f"backend: {getattr(result, 'error', None) or 'no merge marker'}; "
                f"output={output[:2000]!r}"
            ),
            status_code=502,
        )


async def _restart_hermes_backend(run_fn: Any) -> None:
    """Restart the resident backend so it re-reads its process configuration.

    Hermes fixes its environment (the model API key among it) and its MCP
    servers when the process starts, so a change to either reaches a running
    backend only through a restart. Everything it reads per session is applied
    in place instead (:func:`_apply_hermes_profile_in_place`).

    Restarting ends every turn running in the backend, so callers wait until
    none is (``wait_until_backend_idle``) before calling this.

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
    runtime_identity: dict[str, Any],
) -> None:
    await install_verified_text_script(
        sandbox,
        path=path,
        content=content,
        runtime_identity=runtime_identity,
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

    Hermes' own session titles are switched off
    (``auxiliary.title_generation.enabled: false``, the vendor's documented
    switch in its configuration guide). Hermes titles a session with two model
    calls on every conversation's first turn, and nothing in the platform reads
    that title: conversation titles are the platform's own.
    """
    model_base_url = _resolve_hermes_openai_base_url(model_access)
    if not model_base_url:
        raise APIError(
            code="HERMES_MODEL_BASE_URL_NOT_CONFIGURED",
            message="OpenAI-compatible model base_url not configured for hermes template",
            status_code=500,
        )
    model_name = str(model_access.model_name or "").strip()
    if not model_name:
        raise APIError(
            code="HERMES_MODEL_NAME_NOT_CONFIGURED",
            message="model_name not configured for hermes template",
            status_code=500,
        )
    provider_slug = (
        re.sub(r"[^a-z0-9_-]+", "-", _HERMES_CUSTOM_PROVIDER_NAME.lower()).strip("-")
    )
    payload: dict[str, Any] = {
        "model": {
            "default": model_name,
            "provider": f"custom:{provider_slug}",
        },
        "custom_providers": [
            {
                "name": _HERMES_CUSTOM_PROVIDER_NAME,
                "base_url": model_base_url,
                "key_env": _HERMES_MODEL_API_KEY_ENV,
                "model": model_name,
                "api_mode": _HERMES_DEFAULT_API_MODE,
            }
        ],
        "auxiliary": {"title_generation": {"enabled": False}},
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


def _build_hermes_config_defaults() -> dict[str, Any]:
    """User-overridable profile yaml (setdefault).

    The platform base (terminal/approvals/security). Anything the user later
    sets via ``hermes config set …`` is preserved.
    """
    return {
        "terminal": {"backend": "local"},
        "approvals": {"mode": "off", "cron_mode": "approve"},
        "security": {"tirith_enabled": False},
    }


def _build_hermes_soul_content(template: Any) -> str:
    """The Assistant's system prompt as Hermes' ``SOUL.md``, or ``""`` for none.

    Hermes documents SOUL.md as the agent's identity: slot one of its system
    prompt, replacing the built-in "You are Hermes Agent" text while the rest of
    its prompt stays. That is the vendor's own place for who the agent is, so
    the platform's system prompt lands there verbatim, with no wrapper.
    """

    content = str(getattr(template, "system", None) or "").strip()
    return f"{content}\n" if content else ""


def _resolve_hermes_openai_base_url(
    model_access: ResolvedModelAccess,
) -> str:
    base_url = _normalize_hermes_openai_base_url(model_access.base_url or "")
    if _looks_anthropic_compatible_base_url(base_url):
        raise APIError(
            code="HERMES_MODEL_BASE_URL_NOT_OPENAI_COMPATIBLE",
            message=(
                "Hermes requires an OpenAI-compatible model base URL; the "
                "Environment's model connection names an Anthropic-compatible one"
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
