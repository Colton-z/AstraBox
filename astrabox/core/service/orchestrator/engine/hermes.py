"""Hermes engine adapter using its TUI gateway JSON-RPC over ``hermes serve``.

Each (user, assistant) profile owns one resident ``hermes serve`` backend
inside the Assistant's sandbox — the vendor's own multi-session runtime — and
every conversation of that profile is one ``session.create``/``session.resume``
on it (decision record: ``docs/maintainers/hermes-runtime-preparation.md``).
The host reaches the backend's WebSocket through the sandbox endpoint
(``docs/maintainers/hermes-transport.md``), and a conversation is read through
the runtime contract by :mod:`.hermes_runtime_client`. Model credentials
follow the separately configured outbound Vault path.

Preparation for this engine is therefore the profile's resident gateway, not a
prepared slot: the Agent-row slot machinery
(``agent/prepared_slots.py``) is keyed by an Agent and fingerprints anonymous,
unclaimed units, while every Hermes unit is owned by its profile from birth
(home path, workload account, MCP deployment id all embed that identity), so
the adapter keeps the default ``prepare_runtime`` refusal from ``EngineAdapter``.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.runtime.skill_sources import normalize_skills, skill_cache_dir

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
from typing import Any, Awaitable, Callable, Literal, Mapping

from astrabox.common.logger.logger_factory import get_logger
from astrabox.config.release_images import release_image
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineCapabilityManifest,
    EngineKind,
    EngineStartupContext,
    EngineStartupMaterialRequest,
    RuntimeStateSaveRequired,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
)
from astrabox.core.service.orchestrator.engine.extensions import EngineExtensions, PackageKind
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.engine.registry import (
    register_engine_adapter,
)
from astrabox.core.service.orchestrator.engine.emissions import ChildResourceFact
from astrabox.core.service.orchestrator.engine.hermes_a2a import (
    assistant_a2a_peers,
    assistant_managed_config,
)
from astrabox.core.service.orchestrator.engine.hermes_cron import CronRunOutput
from astrabox.core.service.orchestrator.engine.hermes_client import HermesTuiRpcError
from astrabox.core.service.orchestrator.engine.hermes_gateway import (
    HermesGatewayHandle,
    gateway_handle_for_profile,
    resolve_gateway_handle,
    wait_until_backend_idle,
)
from astrabox.core.service.orchestrator.engine.hermes_runtime_client import HermesRuntimeClient
from astrabox.core.service.orchestrator.engine.runtime import RuntimeCursor, RuntimeProtocolError, initialize_runtime_client
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    resolve_sandbox_endpoint,
)
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.runtime.sandbox_script_writer import (
    install_verified_text_script,
)
from astrabox.core.service.orchestrator.runtime.storage import (
    cached_package_dirs,
    prepare_agent_runtime_plugin_cache,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
    get_underlying_sandbox,
)
from astrabox.seams.model import ResolvedModelAccess
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    runtime_mcp_servers_for_binding,
    binding_mcp_server_definitions,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    prepare_agent_runtime_skill_cache,
    normalize_runtime_identity,
    plan_assistant_profile_identity,
)
from astrabox.seams.sandbox_data import SandboxCommandResult, SandboxCommands


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
# Present exactly when the Assistant has Skills: the runtime Skill cache the
# merge copies into the profile's ``skills/astrabox`` category. Hermes indexes
# Skills when it builds a session, so they need no restart.
_HERMES_SKILLS_CACHE_ENV = "ASTRABOX_HERMES_SKILLS_CACHE"
# Present exactly when the Assistant has packages: JSON ``[{"kind", "key",
# "source"}]``, each a cached plugin directory the merge places at
# ``plugins/<key>``, enables and installs the Python dependencies of.
_HERMES_PLUGINS_ENV = "ASTRABOX_HERMES_PLUGINS"
# Hermes' managed scope (``hermes_cli/managed_scope.py``): a config.yaml that
# wins over the profile's, key by key. The owner's ``engine_options.config``
# is written there, root-owned and outside the profile, so the agent cannot
# change it; the profile setup writes it from ``_HERMES_MANAGED_CONFIG_ENV``
# and removes it when that is absent.
_HERMES_MANAGED_DIR = "/etc/astrabox/hermes"
_HERMES_MANAGED_CONFIG_ENV = "ASTRABOX_HERMES_MANAGED_CONFIG"
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
    {
        _HERMES_CONFIG_OVERWRITE_ENV,
        _HERMES_CONFIG_DEFAULTS_ENV,
        _HERMES_SOUL_B64_ENV,
        _HERMES_SKILLS_CACHE_ENV,
    }
)
# The part of config.yaml Hermes reads only when the process starts: MCP
# servers are discovered once into a process-wide registry, and a session's
# agent snapshots its tools from it. The vendor's live alternative,
# ``reload.mcp``, tears that registry down under every session and fails
# their in-flight MCP calls, so it is no gentler than a restart. The timezone
# is resolved once per process too (``hermes_time._timezone_entry``), and the
# scheduler reads schedules in it.
_HERMES_PROCESS_CONFIG_KEYS = ("mcp_servers", "timezone", "a2a_agents")

#: The config.yaml keys the platform writes or reconciles. The owner's
#: managed configuration wins over the profile, so it may not name them.
_HERMES_PROTECTED_CONFIG_KEYS = (
    "model",
    "providers",
    "custom_providers",
    "mcp_servers",
    "terminal",
    "timezone",
    "skills",
    "plugins.enabled",
    "plugins.disabled",
    "display.busy_input_mode",
)

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
    """Block until the box's command service runs a command, or fail loud.

    Each attempt is a no-op through the box's commands face with a five-second
    deadline of its own. ``timeout_s`` defaults to the module constant,
    resolved at call time.
    """
    effective_s = _HERMES_INBOX_READY_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + float(effective_s)
    last_exc: BaseException | None = None
    while True:
        try:
            await sandbox.commands.run("true", timeout=5.0)
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


class HermesEngineAdapter(EngineAdapter):
    """EngineAdapter for Hermes — declares and connects its gateway client."""

    def process_disposal(self) -> Any:
        return _HERMES_PROCESS_DISPOSAL

    def stored_child_resource_facts(
        self,
        *,
        native_session_key: str | None,
        runtime_state: bytes | None,
        live_frames: list[dict[str, Any]],
    ) -> list[ChildResourceFact]:
        from astrabox.core.service.orchestrator.engine.hermes_child_transcript import (
            stored_child_resource_facts,
        )

        return stored_child_resource_facts(
            native_session_key=native_session_key,
            runtime_state=runtime_state,
            live_frames=live_frames,
        )

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
        native_state = child_run.get("_native_state")
        if isinstance(native_state, dict) and "ended_at" in native_state:
            return native_state["ended_at"] is None
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
    def engine_client_type(self) -> type[Any]:
        return HermesRuntimeClient

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
            extensions=HERMES_EXTENSIONS,
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

    def startup_environment_scope(self) -> Literal["process", "session"] | None:
        return "session"

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

        engine_client: HermesRuntimeClient | None = None
        engine_manifest: EngineCapabilityManifest | None = None
        runtime_identity = context.runtime_identity
        profile_prepared = False
        try:
            await _await_hermes_inbox_ready(sandbox)
            if not runtime_identity:
                raise APIError(
                    code="HERMES_PROFILE_INVALID",
                    message="platform startup supplied no Assistant runtime identity",
                    status_code=500,
                )
            spawn_fingerprint = None
            # An output observer only reads a running backend; preparing the
            # profile could restart it under the conversations it serves.
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

                profile_prepared = True
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
                session_env=context.session_env,
            )
            if not context.resume_session_key:
                await self._save_runtime_state(sandbox.commands, runtime_identity)
            engine_manifest = await initialize_runtime_client(
                engine_client,
                engine_kind="assistant",
                resume_session_key=context.resume_session_key,
            )
        except BaseException as exc:
            if engine_client is not None:
                with contextlib.suppress(BaseException):
                    await engine_client.close()
            if isinstance(exc, RuntimeProtocolError):
                # A protocol mismatch says nothing about native execution.
                # Do not quiesce a live backend to make the attachment fail.
                raise
            if profile_prepared and workspace_plan.operation == "runtime_start" and runtime_identity:
                try:
                    await self.quiesce_and_save_runtime_state(sandbox, runtime_identity=runtime_identity)
                except BaseException as save_error:
                    raise RuntimeStateSaveRequired(extract_sandbox_id(sandbox), runtime_identity) from save_error
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"hermes runtime start failed: {exc}",
                status_code=502,
                retryable=False if isinstance(exc, HermesTuiRpcError) and exc.code in {4000, -32602} else None,
            ) from exc

        assert engine_client is not None
        assert engine_manifest is not None
        return SessionRuntime(
            session_id=context.session_id,
            agent=None,
            sandbox=sandbox,
            sandbox_id=extract_sandbox_id(sandbox),
            engine_session_key=engine_client.engine_session_key,
            terminal_cwd=context.cwd,
            permission_mode=None,
            engine_kind="assistant",
            engine_client=engine_client,
            engine_manifest=engine_manifest,
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
        session_env: dict[str, str] | None = None,
    ) -> HermesRuntimeClient:
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
        return await HermesRuntimeClient.connect(
            gateway=gateway,
            platform_session_id=session_id,
            resume_session_key=resume_session_key,
            session_env=session_env,
            read_cron_run_output=_cron_run_output_reader(sandbox, identity),
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
        # `astrabox-hermes-serve` starts the backend under supervisord, which
        # in a freshly initialized box happens only once its profile is
        # published, and `astrabox-hermes-forward` publishes it once it
        # answers. This resolves an address rather than starting anything.
        endpoint = await resolve_sandbox_endpoint(sandbox, HERMES_BACKEND_PORT)
        return await resolve_gateway_handle(
            url=hermes_backend_ws_url(endpoint.url),
            headers=hermes_backend_headers(
                endpoint.headers, _hermes_backend_token(normalized)
            ),
            sandbox_id=sandbox_id,
            profile_key=profile_ref["profile_key"],
            spawn_fingerprint=spawn_fingerprint,
            wait_until_answering=lambda: _wait_until_backend_answers(sandbox.commands),
        )

    async def quiesce_and_save_runtime_state(
        self, sandbox: Any, *, runtime_identity: dict[str, Any]
    ) -> None:
        commands: SandboxCommands = sandbox.commands
        phase = "hermes_close_restarts"
        try:
            probe = (
                "from pathlib import Path; import sys; "
                "paths=[Path(p) for p in sys.argv[1:]]; "
                "present=False\n"
                "for p in paths:\n"
                " try: p.lstat(); present=True\n"
                " except FileNotFoundError: pass\n"
                "print('HERMES_NATIVE_STATE_PRESENT' if present else 'HERMES_NATIVE_STATE_NOT_STARTED')"
            )
            observed = await commands.run(shlex.join([
                "python3", "-c", probe, "/run/astrabox-hermes/profile-ready.json",
                posixpath.join(runtime_identity["config_dir"], "state.db"),
                posixpath.join(runtime_identity["config_dir"], "astrabox-runtime-state-restored.json"),
                posixpath.join(runtime_identity["config_dir"], "astrabox-initial-state-recovery.json"),
            ]))
            if observed.exit_code != 0:
                raise RuntimeError("native state initialization could not be observed")
            if observed.output.strip() == "HERMES_NATIVE_STATE_NOT_STARTED":
                return
            # A restart already running finishes first; none starts after.
            closed = await commands.run(
                "mkdir -p " + shlex.quote(posixpath.dirname(_HERMES_PAUSING_MARKER))
                + " && exec flock " + shlex.quote(_HERMES_SUPERVISE_LOCK)
                + " touch " + shlex.quote(_HERMES_PAUSING_MARKER),
                # Waits out a restart holding the lock, which supervisor's
                # stop waits bound.
                timeout=None,
            )
            if closed.exit_code != 0:
                raise RuntimeError("the box did not record that its restarts are closed")
            for program, phase in (
                # The scheduler writes the same native state the save reads.
                (_HERMES_SCHEDULER_PROGRAM, "hermes_stop_scheduler"),
                ("astrabox-hermes-state-mirror", "hermes_stop_state_mirror"),
                (_HERMES_BACKEND_PROGRAM, "hermes_stop_backend"),
            ):
                # A failed mirror can already have exited. Supervisor's
                # read-back, not the stop command's result, confirms no writer.
                await commands.run("supervisorctl stop " + shlex.quote(program))
                status = await commands.run(
                    "bash -lc " + shlex.quote(
                        "status=0; supervisorctl status " + shlex.quote(program)
                        + '; status=$?; if [ "$status" -ne 0 ] '
                        '&& [ "$status" -ne 3 ]; then exit "$status"; fi'
                    ),
                )
                if (
                    status.exit_code != 0
                    or re.fullmatch(
                        re.escape(program) + r"\s+(?:STOPPED|EXITED|FATAL)(?:\s+[^\n]*)?",
                        status.output.strip(),
                    ) is None
                ):
                    raise RuntimeError("supervisor did not confirm a stopped process")
            phase = "hermes_native_state_save"
            await self._restore_runtime_state(commands, runtime_identity)
            await self._save_runtime_state(commands, runtime_identity)
        except Exception as exc:
            raise APIError(
                code="ASSISTANT_WORKSPACE_CONVERGENCE_FAILED",
                message=f"Hermes runtime state barrier failed during {phase}",
                status_code=503,
                data={"phase": phase},
            ) from exc

    async def _restore_runtime_state(
        self, commands: SandboxCommands, runtime_identity: dict[str, Any]
    ) -> None:
        """Finish a fenced initial recovery before saving a retained cold profile."""
        env_path = posixpath.join(runtime_identity["config_dir"], _HERMES_PROFILE_ENV_FILENAME)
        command = shlex.join([
            "runuser", "-u", runtime_identity["linux_user"], "--", "bash", "-c",
            'set -euo pipefail; source "$1"; exec "$HERMES_VENV/bin/python" "$2" restore --profile-env "$1"',
            "astrabox-hermes-state-restore", env_path, _HERMES_STATE_BOOTSTRAP,
        ])
        result = await commands.run(command, timeout=None)
        if result.exit_code != 0:
            raise RuntimeError("native state restore did not complete before release")

    async def _save_runtime_state(
        self, commands: SandboxCommands, runtime_identity: dict[str, Any]
    ) -> None:
        """Confirm the complete native/profile snapshot through the installed mirror.

        The mirror serializes saves and uses the supplier's WAL-consistent
        backup, so a new conversation can be saved while siblings are running.
        """
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
        # A save is as long as the profile's native state is large.
        result = await commands.run(command, timeout=None)
        markers = [
            line for line in result.output.splitlines()
            if re.fullmatch(r"HERMES_STATE_SAVE_COMPLETE snapshot_id=\S+", line)
        ]
        if result.exit_code != 0 or len(markers) != 1:
            raise RuntimeError("native state save did not confirm its snapshot")

    async def list_scheduled_jobs(
        self, sandbox: Any, *, runtime_identity: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        """The profile's cron jobs, from Hermes' own store (``cron.jobs.list_jobs``).

        Runs as the profile's owner with its environment, as the state save
        does, so Hermes resolves the profile's ``HERMES_HOME`` and nothing in
        the store is touched by another account. Each job keeps Hermes' field
        names; only the ones an owner reads are carried.
        """

        profile_env = posixpath.join(runtime_identity["config_dir"], _HERMES_PROFILE_ENV_FILENAME)
        command = shlex.join([
            "runuser", "-u", runtime_identity["linux_user"], "--", "env",
            f"HOME={runtime_identity['home_dir']}",
            "bash", "--noprofile", "--norc", "-c",
            'set -euo pipefail; source "$1"; exec "$HERMES_VENV/bin/python" -c "$2"',
            "astrabox-hermes-cron-list", profile_env, _CRON_JOBS_LISTER,
        ])
        result = await sandbox.commands.run(command)
        output = result.output
        # Hermes' own log lines can land next to the answer, so it is found by
        # its marker and read as one JSON value from there.
        start = output.rfind(_CRON_JOBS_MARKER)
        if result.exit_code != 0 or start < 0:
            raise RuntimeError("Hermes did not list its cron jobs: " + output.strip()[-400:])
        jobs, _end = json.JSONDecoder().raw_decode(output, start + len(_CRON_JOBS_MARKER))
        return [
            {field: job.get(field) for field in _CRON_JOB_FIELDS}
            for job in jobs
            if isinstance(job, dict)
        ]


# The shared assistant workspace root baked into the Hermes runtime image (WORKDIR).
# Per-(user, assistant) profile homes live under it; used as the container cwd when
# the workspace plan carries none.
_HERMES_WORKSPACE_ROOT = "/home/conversations"


#: What an Assistant may add to Hermes at the pinned commit
#: (``docs/maintainers/engine-extension-contract.md``). Skills and plugins are
#: placed under an ``astrabox`` category the platform owns entirely, so nothing
#: the agent or its owner created in the profile is touched, and a plugin's key
#: is ``astrabox/<directory>`` (``hermes_cli/plugins_discovery.py``). Hermes
#: discovers plugins once per process and reads its configuration documents at
#: start, so a change to either restarts the backend once nothing is running;
#: Skills are read when a conversation starts.
#:
#: Gateway event hooks (``$HERMES_HOME/hooks``) are not offered: they fire for
#: sessions of Hermes' messaging gateway, which here runs only the scheduler.
#: A plugin's hooks run in every agent loop.
HERMES_EXTENSIONS = EngineExtensions(
    mcp_transports=frozenset({"stdio", "http", "sse"}),
    skills_target="{config}/skills/astrabox",
    packages=(
        PackageKind(
            name="hermes-plugin",
            label="Hermes plugin",
            target="{config}/plugins/astrabox",
            takes_effect="restart",
            installed_by_engine=True,
            # Native PM resolves the core and plugin dependency graph together.
            # Its pinned GitHub wheels redirect to the release asset host.
            installer_hosts=(
                "pypi.org", "files.pythonhosted.org", "github.com",
                "release-assets.githubusercontent.com",
            ),
            help="A Hermes plugin directory with plugin.yaml: tools, hooks, slash commands, "
            "Skills or system prompt sections. Hermes' package manager prepares its Python "
            "dependencies together with Hermes' own dependency graph.",
        ),
        PackageKind(
            name="agent-plugin",
            label="Agent Plugins package",
            target="{config}/plugins/astrabox",
            takes_effect="restart",
            installed_by_engine=True,
            help="An Agent Plugins v1.0 package: plugin.json with Skills and an mcp.json.",
        ),
    ),
    native_config=(
        {
            "key": "config",
            "type": "object",
            "label": "Hermes config.yaml",
            "help": "Hermes configuration the agent cannot change, applied through Hermes' "
            "managed scope over the profile's config.yaml: nested keys merge, lists and values "
            "replace. Takes effect when the backend restarts. Skills settings are not read "
            "from the managed scope; Skills come from the Skills field.",
            "protected_keys": list(_HERMES_PROTECTED_CONFIG_KEYS),
        },
    ),
)


def _resolve_hermes_profile_ref(
    *,
    user_id: str | None,
    assistant_id: str | None,
) -> dict[str, str]:
    # This is a registry identity, not a directory or a vendor profile name.
    resolved_user_id = str(user_id or "").strip()
    if not resolved_user_id:
        raise APIError(
            code="HERMES_PROFILE_INVALID",
            message="Hermes profile requires user_id",
            status_code=500,
        )
    safe_assistant_id = _safe_hermes_profile_segment(
        assistant_id,
        label="assistant_id",
    )
    return {
        "user_id": resolved_user_id,
        "assistant_id": safe_assistant_id,
        "profile_key": f"{resolved_user_id}:{safe_assistant_id}",
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
    commands: SandboxCommands = sandbox.commands

    overwrite_blob = _build_hermes_config_overwrite(
        deployment_settings,
        template=template,
        model_access=model_access,
        mcp_deployment_id=mcp_deployment_id,
    )
    if assistant_id := getattr(template, "assistant_id", None):
        overwrite_blob["a2a_agents"] = await assistant_a2a_peers(
            assistant_id=assistant_id,
            binding_id=mcp_deployment_id,
            proxy_base_url=str(getattr(deployment_settings, "mcp_proxy_base_url", "") or ""),
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
    env.update(await _prepare_hermes_extensions(sandbox, template, normalized))
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
        commands,
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
        owner_user=normalized["linux_user"],
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
    # The setup merges the profile and places its packages, so it is as long
    # as what the Assistant carries.
    result = await commands.run(
        "bash -lc "
        + shlex.quote(
            'set -euo pipefail; source "$1"; exec bash "$2" "$3"'
        )
        + " _ "
        + shlex.quote(profile_env_path)
        + " "
        + shlex.quote(_HERMES_PROFILE_SETUP_SCRIPT_PATH)
        + " "
        + shlex.quote(normalized["workspace_source_dir"]),
        timeout=None,
    )
    output = result.output
    if result.exit_code != 0 or "HERMES_PROFILE_READY" not in output:
        raise APIError(
            code="HERMES_PROFILE_SETUP_FAILED",
            message=(
                "failed to prepare Hermes profile: "
                f"{_hermes_failure(result)}; output={output[:2000]!r}"
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
    initialized = await commands.run(
        "bash -lc " + shlex.quote(
            'set -euo pipefail; exec "$HERMES_VENV/bin/python" "$1" publish '
            '--profile-env "$2" --sandbox-id "$3"'
        ) + " _ " + shlex.quote(_HERMES_STATE_BOOTSTRAP)
        + " " + shlex.quote(profile_env_path) + " " + shlex.quote(sandbox_id),
        timeout=None,
    )
    if initialized.exit_code != 0 or "HERMES_PROFILE_INITIALIZED" not in initialized.output:
        raise APIError(
            code="HERMES_PROFILE_SETUP_FAILED",
            message="failed to publish this box's Hermes profile initialization",
            status_code=502,
        )
    # A freshly initialized box has not started Hermes yet: publishing just
    # released `astrabox-hermes-serve`, which merges this profile and then
    # starts the backend under it.
    running = "fresh=0" in initialized.output
    if running and standing == "PROCESS_UNCHANGED":
        # The recorded digest already names this process configuration.
        await _apply_hermes_profile_in_place(
            commands, normalized=normalized, profile_env_path=profile_env_path
        )
        return process_digest
    if running and standing == "PROCESS_CHANGED":
        await wait_until_backend_idle()
        await _restart_hermes_backend(commands)
    await install_verified_text_script(
        sandbox,
        path=process_digest_path,
        content=process_digest,
        runtime_identity=normalized,
        owner_user=normalized["linux_user"],
        mode=0o600,
        error_code="HERMES_PROFILE_ENV_INSTALL_FAILED",
        error_message="failed to record the Hermes backend's process configuration",
    )
    return process_digest


async def _prepare_hermes_extensions(
    sandbox: Any, template: Any, normalized: dict[str, Any]
) -> dict[str, str]:
    """Fetch the Assistant's Skills and packages; return the profile env naming them.

    The sources are cloned into the box's runtime caches as an Agent's are,
    and the in-image merge places them in the profile
    (``scripts/runtime/hermes_config_merge.py``). The owner's native
    configuration is carried to the profile setup, which writes it to Hermes'
    managed scope.
    """

    skills = normalize_skills(getattr(template, "skills", None))
    await asyncio.gather(
        prepare_agent_runtime_skill_cache(get_underlying_sandbox(sandbox), skills),
        prepare_agent_runtime_plugin_cache(
            sandbox, template, get_underlying_sandbox_fn=get_underlying_sandbox
        ),
    )
    env = {
        "HERMES_MANAGED_DIR": _HERMES_MANAGED_DIR,
    }
    if skills:
        env[_HERMES_SKILLS_CACHE_ENV] = skill_cache_dir(skills)
    plugins = _hermes_plugin_placements(template)
    if plugins:
        env[_HERMES_PLUGINS_ENV] = json.dumps(plugins, ensure_ascii=False, sort_keys=True)
    managed = dict(getattr(template, "engine_options", None) or {}).get("config")
    if managed and getattr(template, "assistant_id", None):
        managed = assistant_managed_config(managed)
    if managed:
        env[_HERMES_MANAGED_CONFIG_ENV] = json.dumps(managed, ensure_ascii=False, sort_keys=True)
    return env


_HERMES_PLUGIN_DIRECTORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _hermes_plugin_placements(template: Any) -> list[dict[str, str]]:
    """Where each cached package directory goes: ``plugins/astrabox/<directory>``.

    The directory keeps its own name — the repository's for a package at its
    root — which is how Hermes keys a plugin in a category
    (``astrabox/<directory>``), so two packages may not share one.
    """

    placements: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for package, path, source in cached_package_dirs(template):
        kind = str(package.get("kind") or "")
        if HERMES_EXTENSIONS.package_kind(kind) is None:
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"Hermes does not load packages of kind {kind!r}",
                status_code=400,
            )
        named = str(package.get("url") or "") if path == "." else path
        directory = posixpath.basename(named.rstrip("/")).removesuffix(".git")
        if _HERMES_PLUGIN_DIRECTORY.fullmatch(directory) is None:
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"package directory {directory!r} is not a Hermes plugin name",
                status_code=400,
            )
        if directory in seen:
            raise APIError(
                code="PACKAGE_INVALID",
                message=(
                    f"packages {seen[directory]!r} and {package.get('url')!r} both provide a "
                    f"plugin directory named {directory!r}; Hermes keys plugins by directory"
                ),
                status_code=400,
            )
        seen[directory] = str(package.get("url") or "")
        placements.append({"kind": kind, "key": f"astrabox/{directory}", "source": source})
    return placements


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
#: A cold attachment includes Hermes PM resolving and installing the owner's
#: plugin dependencies before the backend serves requests. Keep that native
#: preparation inside one bounded wait; a healthy cold install can exceed 120s.
_BACKEND_ANSWER_BUDGET_SECONDS = 300


async def _wait_until_backend_answers(commands: SandboxCommands) -> None:
    """Wait, inside the box, until the backend answers through its published port.

    A provider's endpoint can accept a WebSocket upgrade for a port nothing
    serves yet and close it afterwards, so a failed attachment does not say
    whether the backend is starting. Any HTTP status through the forwarder does:
    the forwarder opens the port only once the backend answers, and relays to
    it, so an answer also covers a backend restarted behind a forwarder that
    stayed up.
    """
    probe = (
        f"deadline=$((SECONDS + {_BACKEND_ANSWER_BUDGET_SECONDS})); "
        "until code=$(curl -sS -o /dev/null -m 3 -w '%{http_code}' "
        f"http://127.0.0.1:{HERMES_BACKEND_PORT}/api/auth/me 2>/dev/null) "
        '&& [ "$code" != 000 ]; do '
        'if [ "$SECONDS" -ge "$deadline" ]; then exit 124; fi; sleep 0.2; done'
    )
    result = await commands.run(
        "bash -c " + shlex.quote(probe), timeout=_BACKEND_ANSWER_BUDGET_SECONDS + 30,
    )
    if result.exit_code != 0:
        raise APIError(
            code="HERMES_BACKEND_NOT_READY",
            message=(
                "the Assistant's Hermes backend did not answer within "
                f"{_BACKEND_ANSWER_BUDGET_SECONDS}s (probe exit {result.exit_code})"
            ),
            status_code=503,
        )


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
#: Hermes' gateway with no messaging platform: it runs the profile's cron jobs
#: and reads the same environment and MCP configuration as the backend.
_HERMES_SCHEDULER_PROGRAM = "astrabox-hermes-scheduler"
#: Held by a profile restart and by the pause that ends restarts, so the two
#: never interleave in the box.
_HERMES_SUPERVISE_LOCK = "/run/astrabox/hermes-supervise.lock"
#: Written under that lock when the box's Assistant starts pausing; a restart
#: that finds it does not start the services the pause is stopping.
_HERMES_PAUSING_MARKER = "/run/astrabox/hermes-pausing"
_HERMES_RESTART_REFUSED_EXIT = 75


async def _hermes_process_standing(
    commands: SandboxCommands, *, env_path: str, digest_path: str, digest: str
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
    result = await commands.run(
        "bash -lc " + shlex.quote(probe) + " _ " + shlex.quote(env_path)
        + " " + shlex.quote(digest_path) + " " + shlex.quote(digest)
    )
    output = result.output
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
    commands: SandboxCommands, *, normalized: dict[str, Any], profile_env_path: str
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
    result = await commands.run(command, timeout=None)
    output = result.output
    if result.exit_code != 0 or not any(
        marker in output for marker in _HERMES_CONFIG_MERGE_MARKERS
    ):
        raise APIError(
            code="HERMES_PROFILE_SETUP_FAILED",
            message=(
                "failed to apply the changed Hermes profile to the running "
                f"backend: {_hermes_failure(result, missing='no merge marker')}; "
                f"output={output[:2000]!r}"
            ),
            status_code=502,
        )


async def _restart_hermes_backend(commands: SandboxCommands) -> None:
    """Restart the resident backend so it re-reads its process configuration.

    Hermes fixes its environment (the model API key among it) and its MCP
    servers when the process starts, so a change to either reaches a running
    backend only through a restart. Everything it reads per session is applied
    in place instead (:func:`_apply_hermes_profile_in_place`).

    Restarting ends every turn running in the backend, so callers wait until
    none is (``wait_until_backend_idle``) before calling this. A restart and
    the pause that saves the box's native state take the same lock in the box,
    and once a pause has begun no restart runs there.

    The host is not assembling a capability here — the service is the image's,
    supervised by the image — it is telling a service its inputs moved. The
    attach that follows spends its bounded wait on the backend coming back.
    """

    # Each program may take its supervisor stop wait before starting again.
    restart = (
        f"test ! -e {shlex.quote(_HERMES_PAUSING_MARKER)} || exit {_HERMES_RESTART_REFUSED_EXIT}; "
        f"exec supervisorctl restart {_HERMES_BACKEND_PROGRAM} {_HERMES_SCHEDULER_PROGRAM}"
    )
    result = await commands.run(
        "bash -lc "
        + shlex.quote(
            "set -euo pipefail; mkdir -p "
            + shlex.quote(posixpath.dirname(_HERMES_SUPERVISE_LOCK))
            + "; exec flock " + shlex.quote(_HERMES_SUPERVISE_LOCK)
            + " bash -c " + shlex.quote(restart)
        ),
        timeout=None,
    )
    if result.exit_code == _HERMES_RESTART_REFUSED_EXIT:
        raise APIError(
            code="ASSISTANT_WORKSPACE_NOT_READY",
            message=(
                "the Assistant is pausing, so its Hermes backend is not "
                "restarted for a changed profile"
            ),
            status_code=409,
        )
    output = result.output
    if result.exit_code != 0 or "started" not in output:
        raise APIError(
            # The registered code for "the resident gateway is not up under
            # the profile this host just wrote", which is exactly what a
            # refused restart leaves behind. A separate code would name the
            # same owner, the same phase and the same client action.
            code="HERMES_GATEWAY_START_FAILED",
            message=(
                "the Hermes backend did not restart for a changed profile, so it "
                f"would keep serving the old one: "
                f"{_hermes_failure(result, missing='no start marker')}; "
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
        owner_user=runtime_identity["linux_user"],
        mode=0o600,
        error_code="HERMES_PROFILE_ENV_INSTALL_FAILED",
        error_message="failed to install Hermes profile env file",
    )


_CRON_JOB_ID = re.compile(r"[A-Za-z0-9_.-]+")

#: Run in the box with Hermes' Python: the newest saved run of one job, and
#: whether its response is the quiet marker by Hermes' own autonomous-lane rule
#: (``[SILENT]`` and the variants ``gateway/response_filters.py`` accepts at the
#: pinned commit), the rule Hermes' scheduler applies before it delivers.
_CRON_RUN_READER = r"""
import json, pathlib, sys
from gateway.response_filters import is_autonomous_silence_response
runs = sorted(pathlib.Path(sys.argv[1]).glob("*.md"), key=lambda path: path.stat().st_mtime)
if runs:
    text = runs[-1].read_text(errors="replace")[:65536]
    response = text.split("## Response", 1)[1] if "## Response" in text else ""
    print(json.dumps({"output": text, "silent": is_autonomous_silence_response(response)}))
"""


#: Lists the profile's jobs through Hermes' own store API, disabled ones included.
_CRON_JOBS_MARKER = "ASTRABOX_HERMES_CRON_JOBS "
_CRON_JOBS_LISTER = r"""
import json, sys
from cron.jobs import list_jobs
jobs = json.dumps(list_jobs(include_disabled=True), default=str)
sys.stdout.write("\nASTRABOX_HERMES_CRON_JOBS " + jobs + "\n")
sys.stdout.flush()
"""
#: The fields of a Hermes job record (``cron/jobs.py`` ``create_job``) an owner reads.
_CRON_JOB_FIELDS = (
    "id", "name", "prompt", "schedule_display", "state", "enabled",
    "next_run_at", "last_run_at", "last_status", "last_error", "created_at", "repeat",
)


def _cron_run_output_reader(
    sandbox: Any, identity: dict[str, Any] | None
) -> Callable[[str], Awaitable[CronRunOutput | None]] | None:
    """Read the newest result Hermes saved for one cron job, inside the box.

    Hermes writes each run to ``$HERMES_HOME/cron/output/<job_id>/<time>.md``
    (``cron/jobs.py`` at the pinned commit) and serves no endpoint for it.
    """

    if sandbox is None or not identity:
        return None
    root = posixpath.join(normalize_runtime_identity(identity)["config_dir"], "cron", "output")

    async def read(job_id: str) -> CronRunOutput | None:
        if not _CRON_JOB_ID.fullmatch(job_id) or job_id in {".", ".."}:
            return None
        result = await sandbox.commands.run(
            shlex.join([
                "bash", "-c", 'exec "$HERMES_VENV/bin/python" -c "$1" "$2"',
                "hermes-cron-output", _CRON_RUN_READER, posixpath.join(root, job_id),
            ]),
        )
        if result.exit_code != 0:
            return None
        printed = [line for line in result.stdout.splitlines() if line.startswith("{")]
        if not printed:
            return None
        run = json.loads(printed[-1])
        return CronRunOutput(output=str(run["output"]), silent=run["silent"] is True)

    return read


def _hermes_failure(result: SandboxCommandResult, *, missing: str = "missing readiness marker") -> str:
    """Why a Hermes command's result is not the answer: its exit status, or ``missing``."""

    if result.exit_code != 0:
        return f"exit_code={result.exit_code}"
    return missing


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

    Hermes' busy-input mode is ``queue`` (``display.busy_input_mode``, the
    "Busy Input Mode" section of the vendor's CLI guide, read on every busy
    submit by ``_load_busy_input_mode`` in ``tui_gateway/server.py``). Its
    default, ``interrupt``, redirects the running turn with a message sent
    during it, or hard-interrupts the turn; under ``queue`` the message waits
    and runs as the next turn. That is the order the platform promises for a
    conversation's inputs, and the order the runtime client reads Hermes'
    turns in (``engine/hermes_runtime_client.py``).

    Hermes' model catalog fetch (``model_catalog.enabled``) stays off: its
    hosts are outside the box's egress policy, and models use the custom
    provider. Package installs (``security.allow_lazy_installs``) are enabled
    for native Hermes plugins, whose declaration admits PyPI and whose
    dependencies the profile merger prepares through Hermes' package manager
    in its per-home environment. Other profiles keep installs disabled.
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
        "display": {"busy_input_mode": "queue"},
        "model_catalog": {"enabled": False},
        "security": {
            "allow_lazy_installs": any(
                plugin["kind"] == "hermes-plugin"
                for plugin in _hermes_plugin_placements(template)
            ),
        },
        # The owner's timezone, which Hermes names in every system prompt and
        # reads cron schedules in. Empty is Hermes' own "unset": the
        # server's local time.
        "timezone": str(getattr(template, "timezone", None) or ""),
    }
    artifacts_enabled = getattr(template, "artifacts_enabled", True)
    if binding_mcp_server_definitions(
        getattr(template, "mcp_servers", None),
        session_source_type=getattr(template, "session_source_type", None),
        agent_id=getattr(template, "agent_id", None),
        assistant_id=getattr(template, "assistant_id", None),
        artifacts_enabled=artifacts_enabled,
    ):
        mcp_servers = runtime_mcp_servers_for_binding(
            getattr(template, "mcp_servers", None),
            proxy_base_url=str(
                getattr(deployment_settings, "mcp_proxy_base_url", "") or ""
            ),
            deployment_id=str(mcp_deployment_id or "").strip(),
            session_source_type=getattr(template, "session_source_type", None),
            agent_id=getattr(template, "agent_id", None),
            assistant_id=getattr(template, "assistant_id", None),
            artifacts_enabled=artifacts_enabled,
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
        # Hermes tells the agent a job created from this kind of session is
        # "local-only" and never reported back (tools/cronjob_job_args.py),
        # which is untrue here: AstraBox reports each run into the
        # conversation that created the job (engine/hermes_cron.py).
        "agent": {"system_prompt": _HERMES_CRON_DELIVERY_NOTE},
    }


_HERMES_CRON_DELIVERY_NOTE = (
    "Scheduled jobs you create with cronjob_manage report each run back into the "
    "conversation that created them, so the user receives the result wherever they "
    "talk to you. Disregard any notice that such a job is local-only or will not be "
    "delivered."
)


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
                "RuntimeProfile's model connection names an Anthropic-compatible one"
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
    """Close a recorded native session on its existing profile attachment."""

    async def dispose_process(
        self,
        *,
        sandbox_id: str,
        engine_session_key: str,
        runtime_cursor: dict | None,
        workspace_ref: dict,
    ) -> None:
        if not engine_session_key:
            return
        profile = _resolve_hermes_profile_ref(
            user_id=workspace_ref.get("user_id"), assistant_id=workspace_ref.get("assistant_id"),
        )
        gateway = gateway_handle_for_profile(sandbox_id=sandbox_id, profile_key=profile["profile_key"])
        if gateway is None:
            # This capability only acts through the attachment this host owns;
            # a different host's cleanup retains its established behavior.
            logger.info("Hermes final disposal has no local profile attachment sandbox=%s", sandbox_id)
            return
        live = await gateway.request("session.active_list", {})
        sessions = live.get("sessions")
        if not isinstance(sessions, list) or any(
            not isinstance(row, dict) or not row.get("id") or not row.get("session_key") for row in sessions
        ):
            raise RuntimeError("Hermes did not report its live native conversation identities")
        if not sessions:
            return
        cursor = RuntimeCursor.from_json(runtime_cursor)
        # Native compression rotates session_key in the same TUI record and
        # continues emitting on its unchanged id. Only this backend's epoch
        # makes the recorded id authoritative across that key rotation.
        epoch, _, session_id = (cursor.generation if cursor is not None else "").rpartition(":")
        if session_id and epoch == gateway.replay_epoch:
            targets = [row for row in sessions if row["id"] == session_id]
        else:
            targets = [row for row in sessions if row["session_key"] == engine_session_key]
        # An absent target is already closed. Every id comes from this scoped
        # backend's list; an older incarnation's cursor cannot select an id.
        for row in targets:
            closed = await gateway.request("session.close", {"session_id": row["id"]})
            if not isinstance(closed.get("closed"), bool):
                raise RuntimeError("Hermes did not confirm native session closure")


_HERMES_PROCESS_DISPOSAL = HermesProcessDisposal()


# Self-register on import. Idempotent — registry guards against duplicates.
register_engine_adapter("assistant", HermesEngineAdapter())
