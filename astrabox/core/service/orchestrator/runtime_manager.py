"""Remote agent runtime manager — thin facade.

Orchestrates runtime lifecycle by delegating to focused modules:
  - runtime.config_resolver: engine-neutral deployment and secret resolution
  - engine adapters: engine-native options and launch configuration
  - runtime.sandbox_client: sandbox SDK connect/kill/renew helpers
  - runtime.interaction: interaction broker HTTP relay
  - runtime.diagnostics: startup failure diagnostics collection
  - runtime.storage: NAS mount, CLAUDE.md injection, JSONL download
"""

import asyncio
import sys
from pathlib import Path
import contextlib
import os
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recovery_ownership import current_recovery
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import parse_iso
from astrabox.core.model import AgentView, SessionState

from astrabox.core.service.orchestrator.runtime.config_resolver import (
    RuntimeConfigResolver,
)
from astrabox.core.service.orchestrator.runtime.diagnostics import (
    is_claude_server_start_timeout_error,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    check_server_health,
    extract_sandbox_id,
    SandboxLifecycleProbeResult,
    get_underlying_sandbox,
    reset_server_session,
    resolve_enhanced_server_endpoint,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    PtyTerminal,
    resolve_execd_endpoint,
)
from astrabox.core.service.orchestrator.runtime.terminal_execution import (
    is_isolated_terminal_execution_id,
)
from astrabox.seams.sandbox import (
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SandboxAllocation,
    SandboxDataPlane,
    SandboxProvider,
    sandbox_for_name,
    sandbox_for_sandbox,
    sandbox_for_template,
)
from astrabox.seams.sandbox_disposal import (
    SANDBOX_DESTRUCTION_NOTHING_NAMED,
    SandboxDestruction,
)
from astrabox.seams.storage import WorkspaceRef, storage_provider

# Built-in provider registration. register_builtin_providers() imports the
# built-in provider modules for their register_* side effects, populating the
# sandbox / dataplane / storage registries this manager reads. Third-party
# plugins can register additional providers at the same seams without touching
# this codebase; an unregistered backend fails loud through sandbox_for_name(...).
from astrabox.providers import register_builtin_providers

register_builtin_providers()
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.assistant_workspace_repository import (
    AssistantWorkspaceRepository,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.core.service.orchestrator.runtime.storage import (
    clone_default_repo,
    mount_assistant_workspace_storage,
)
from astrabox.core.service.orchestrator.session_workspace_plan import (
    ConversationWorkspacePlan,
    RuntimeWorkspacePlan,
    SessionWorkspacePlanner,
)

# Importing the engine package registers all bundled adapters.
from astrabox.core.service.orchestrator.engine import get_engine_adapter
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    bound_engine_client_manifest,
)
from astrabox.core.service.orchestrator.engine.turn_transport import (
    EngineProcessDisposalCapability,
)

# Re-exported: these names belong to this module's import surface for other
# modules and tests, and are not referenced inside this file.
from astrabox.core.service.orchestrator.runtime.models import (  # noqa: F401
    SessionRuntime,
)
from astrabox.core.service.orchestrator.runtime.config_resolver import (  # noqa: F401
    resolve_network_policy,
    is_truthy,
    is_local_mode,
    resolve_runtime_template_name,
)
from astrabox.seams.model import ResolvedModelAccess
from astrabox.seams.egress_credentials import MCPOutboundCredentialResolution
from astrabox.core.service.orchestrator.runtime.diagnostics import (  # noqa: F401
    is_initialize_timeout_error,
    build_runtime_start_error_message,
    extract_command_log_text,
    normalize_diag_text,
    run_sandbox_diag_command,
)

logger = get_logger(__name__)

_RUNTIME_SANDBOX_BINDING_UNSPECIFIED = object()


ProgressCallback = Callable[[str], Coroutine[Any, Any, None]] | None
_ASSISTANT_SANDBOX_DEFAULT_RESOURCE = {"cpu": "8", "memory": "16"}
_SANDBOX_ID_IN_ENDPOINT_RES = (
    re.compile(r"(?:^|[.-])sandbox-([0-9a-f]{32})(?:[.-]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[.-])([0-9a-f]{32})-\d+(?:[.-]|$)", re.IGNORECASE),
    re.compile(r"(?:^|/)sandboxes/([0-9a-f]{32})(?:/|$)", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class StartupAllocationCleanup:
    """Result of releasing one startup allocation without guessing ownership."""

    allocation: SandboxAllocation | None
    released: bool
    record_cleared: bool = True
    destruction: SandboxDestruction | None = None
    detail: str = ""

    @property
    def leaked_sandbox_id(self) -> str | None:
        """A whole box that may still be running with nothing left owning it."""

        if self.released:
            return None
        if self.allocation is None:
            return (
                self.destruction.leaked_sandbox_id
                if self.destruction is not None
                else None
            )
        if self.allocation.scope != "sandbox":
            return None
        return self.allocation.sandbox_id


_PARKED_SANDBOX_STATES = frozenset({"paused", "pausing"})
"""A box that is present, not running, and recoverable under the same id.

Only a deliberate pause produces these: the platform commits the filesystem and
frees the compute, and a resume boots the same sandbox id from that commit. They
therefore answer the two questions a stopped box raises differently, and need a
name of their own:

* *is any execution still in flight here?* — no. Pause kills every process, so a
  turn that was running is over and must be settled. Parked counts as terminal
  for that question (:data:`_INTERACTION_BROKER_TERMINAL_SANDBOX_STATES`).
* *is the resource gone — should the session stop naming this box?* — no. The
  files and the id outlive the pause; dropping the binding here would strand the
  snapshot and cold-create a fresh box, which is the loss the pause was taken to
  prevent (:data:`_SANDBOX_RESOURCE_GONE_STATES`).
"""

_INTERACTION_BROKER_TERMINAL_SANDBOX_STATES = frozenset(
    {
        # Managed-backend lifecycle vocabulary (a remote control plane's names).
        "terminated",
        "failed",
        "error",
        "paused",
        "pausing",
        "stopping",
        # Raw container states for a box that is present but not running: the
        # probe reports these under PROBE_FAILED after a successful
        # inspect (e.g. every session's container after a host reboot). Treating them
        # as terminal is what lets the dead-binding watcher converge a rebooted
        # host's sessions instead of re-probing them forever.
        "exited",
        "dead",
        "stopped",
    }
)

_SANDBOX_RESOURCE_GONE_STATES = _INTERACTION_BROKER_TERMINAL_SANDBOX_STATES - _PARKED_SANDBOX_STATES
"""States that mean the box itself is unrecoverable, so its binding must go.

Derived by subtraction rather than written out, so a state added to the terminal
vocabulary above is gone-by-default: forgetting to classify one leaves a dead
binding converging (safe), where forgetting the other way would leave a session
naming a dead sandbox.
"""


def _sandbox_control_deadline_s() -> float:
    """Deadline (seconds) for one unary sandbox control-plane op (probe/connect/kill).

    Bounds a wedged control-plane call (e.g. a hung dockerd) so it cannot hold a
    session in PROCESSING forever. Applied only to unary ops — never to the exec
    stream path, where an agent turn legitimately streams for many minutes with idle
    gaps and bounding reads would kill live turns. Tunable via
    ``ASTRABOX_SANDBOX_CONTROL_DEADLINE_S`` (default 30; non-positive/invalid → 30).
    """
    raw = str(os.getenv("ASTRABOX_SANDBOX_CONTROL_DEADLINE_S", "") or "").strip()
    try:
        value = float(raw) if raw else 30.0
    except ValueError:
        value = 30.0
    return value if value > 0 else 30.0


class RemoteAgentRuntimeManager:
    _TERMINATE_OP_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        *,
        sessions_repo: Any = None,
        agent_service_getter: Callable[[], Any] | None = None,
        event_broker: Any = None,
    ) -> None:
        self._agent_service_getter = agent_service_getter
        #: sandbox_id → monotonic time of the prewarm sweep's last lease renew.
        self._prewarm_lease_renewed_at: dict[str, float] = {}
        #: agent_id → monotonic time the sweep last scheduled a rebuild for an
        #: Agent with no slot, so a failing build is retried on a cadence.
        self._prewarm_rebuild_scheduled_at: dict[str, float] = {}
        #: The process-wide Session event broker, handed to the platform sinks
        #: an engine publishes resident output through. Absent only in
        #: harnesses that build the manager alone; those sinks warn loudly.
        self.event_broker = event_broker
        self._settings = load_astrabox_settings()
        self._config = RuntimeConfigResolver(self._settings)
        self._runtimes: dict[str, SessionRuntime] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions_repo = sessions_repo or SessionRepository()
        # Full release authority for each allocation created by this process but
        # not yet adopted by a READY Session. Keeping only a sandbox id is unsafe:
        # an isolated Session in a shared box must never fall through to whole-box
        # destruction when the database write that records its scope fails.
        self._pending_startup_allocations: dict[
            str, list[SandboxAllocation]
        ] = {}
        self._quiesced_reason: str | None = None
        # sandbox_id -> persisted backend name; immutable per box, so cache to avoid
        # a session lookup on every by-id control-plane op (e.g. per-turn probe).
        self._sandbox_backend_cache: dict[str, str] = {}
        # An OpenSandbox isolated terminal is interrupted by cancelling the
        # worker that owns its SSE request. TerminalService then deletes that
        # disposable terminal session and persists a replacement; retain the
        # owner task under the durable execution id so the journal-driven
        # interrupt path can start that sequence.
        self._terminal_execution_tasks: dict[str, asyncio.Task[Any]] = {}

    def schedule_agent_runtime_reconciliation(self, agent_id: str) -> bool:
        """Send capacity demand to the Agent's configuration reconciliation.

        Returns whether a reconciliation was started; one already in flight
        for the Agent answers the demand and reports False.
        """

        self._raise_if_quiesced()
        if self._agent_service_getter is None:
            raise RuntimeError("Agent runtime reconciliation is not configured")
        return bool(self._agent_service_getter().schedule_runtime_reconciliation(agent_id))

    def _raise_if_quiesced(self) -> None:
        reason = str(self._quiesced_reason or "").strip()
        if not reason:
            return
        raise APIError(
            code="AGENT_RUNTIME_RELEASING",
            message=f"runtime manager is closing for shutdown: {reason}",
            status_code=503,
        )

    def quiesce(self, *, reason: str) -> None:
        if self._quiesced_reason:
            logger.info(
                "runtime manager already closing for shutdown: previous=%s current=%s",
                self._quiesced_reason,
                reason,
            )
            return
        self._quiesced_reason = str(reason or "shutdown").strip() or "shutdown"
        runtimes = list(self._runtimes.items())
        self._runtimes.clear()
        terminal_tasks = list(self._terminal_execution_tasks.values())
        self._terminal_execution_tasks.clear()
        logger.warning(
            "closing runtime manager for shutdown: reason=%s runtimes=%d",
            reason,
            len(runtimes),
        )
        for _session_id, runtime in runtimes:
            with contextlib.suppress(BaseException):
                if runtime.current_task and not runtime.current_task.done():
                    runtime.current_task.cancel("shutdown")
        for task in terminal_tasks:
            if not task.done():
                task.cancel("shutdown")
        if not runtimes:
            return
        coro = self._disconnect_runtimes_for_quiesce(runtimes, reason=reason)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            task = loop.create_task(coro)
            task.add_done_callback(self._log_quiesce_disconnect_task_done)

    @staticmethod
    def _log_quiesce_disconnect_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            logger.error("shutdown runtime disconnect task was cancelled")
            return
        try:
            task.result()
        except BaseException as exc:
            logger.error("shutdown runtime disconnect task failed: %s", exc, exc_info=True)

    async def _disconnect_runtimes_for_quiesce(
        self,
        runtimes: list[tuple[str, SessionRuntime]],
        *,
        reason: str,
    ) -> None:
        results = await asyncio.gather(
            *(
                self._disconnect_runtime_client(
                    runtime,
                    session_id=session_id,
                    reason=reason,
                )
                for session_id, runtime in runtimes
            ),
            return_exceptions=True,
        )
        for (session_id, _runtime), result in zip(runtimes, results):
            if isinstance(result, BaseException):
                logger.error(
                    "shutdown runtime cleanup failed: session=%s reason=%s err=%s",
                    session_id,
                    reason,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            logger.error(
                "shutdown runtime cleanup completed with disconnect failures: "
                "reason=%s runtimes=%d failures=%d",
                reason,
                len(runtimes),
                len(failures),
            )

    @staticmethod
    async def _await_on_owner_loop(
        runtime: SessionRuntime,
        coro: Any,
        *,
        action: str,
    ) -> Any:
        """Await a client-close coroutine on the loop that owns the client.

        ``client`` / ``engine_client`` are bound to the loop on which they
        were created (captured into ``SessionRuntime.owner_loop`` via
        ``__post_init__``).  Awaiting them on a different loop produces
        "attached to a different loop" errors during shutdown cleanup.
        """
        if not asyncio.iscoroutine(coro):
            return None
        owner_loop = getattr(runtime, "owner_loop", None)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if (
            owner_loop is None
            or owner_loop is current_loop
            or owner_loop.is_closed()
            or not owner_loop.is_running()
        ):
            return await coro
        logger.info(
            "dispatch runtime client close to owner loop: session=%s action=%s",
            runtime.session_id,
            action,
        )
        future = asyncio.run_coroutine_threadsafe(coro, owner_loop)
        return await asyncio.wrap_future(future)

    @staticmethod
    async def _close_runtime_turn_client(runtime: SessionRuntime) -> None:
        engine_client = getattr(runtime, "engine_client", None)
        close_engine = getattr(engine_client, "close", None)
        if callable(close_engine):
            await RemoteAgentRuntimeManager._await_on_owner_loop(
                runtime,
                close_engine(),
                action="engine_client.close",
            )

    @staticmethod
    async def _dispose_runtime_turn_client(runtime: SessionRuntime) -> bool:
        """Permanently stop an engine process when the client supports it.

        ``close`` is intentionally reconnect-safe: it releases platform-side
        sockets but may leave a recoverable process running in the sandbox.
        Engines with a resident per-conversation process expose ``dispose``
        for delete/archive/end. ``True`` means the resident process was
        identified and terminated; ``False`` lets the caller fall back to a
        durable engine turn anchor after a backend restart.
        """
        engine_client = getattr(runtime, "engine_client", None)
        dispose_engine = getattr(engine_client, "dispose", None)
        if not callable(dispose_engine):
            await RemoteAgentRuntimeManager._close_runtime_turn_client(runtime)
            return False
        result = await RemoteAgentRuntimeManager._await_on_owner_loop(
            runtime,
            dispose_engine(),
            action="engine_client.dispose",
        )
        return result is not False

    @staticmethod
    async def _disconnect_runtime_client(
        runtime: SessionRuntime,
        *,
        session_id: str,
        reason: str,
    ) -> None:
        with contextlib.suppress(BaseException):
            if runtime.current_task and not runtime.current_task.done():
                runtime.current_task.cancel(reason)
        try:
            await RemoteAgentRuntimeManager._close_runtime_turn_client(runtime)
        except BaseException as exc:
            logger.error(
                "runtime client disconnect failed: session=%s reason=%s err=%s",
                session_id,
                reason,
                exc,
                exc_info=True,
            )
            raise
        logger.info("runtime client disconnected session=%s reason=%s", session_id, reason)

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def get_runtime(
        self,
        session_id: str,
        *,
        sandbox_id: Any = _RUNTIME_SANDBOX_BINDING_UNSPECIFIED,
    ) -> SessionRuntime | None:
        """Return a resident runtime, optionally checked against durable truth.

        Omitting ``sandbox_id`` is an unconstrained cache lookup. Passing it
        explicitly, including ``None``, asserts the durable binding observed
        by the caller; an unbound durable session must not reuse a resident
        runtime that still names a sandbox.
        """
        runtime = self._runtimes.get(session_id)
        mismatch = self._runtime_sandbox_mismatch(runtime, sandbox_id)
        if mismatch is not None:
            runtime_sandbox_id, expected_sandbox_id = mismatch
            # Who asked, and from where. `expected=<none>` has two causes that
            # this line could not tell apart: a session whose box was released,
            # and one whose pointer has not been written yet — a runtime is
            # resident seconds before startup settles `sandbox_id`. The first
            # must discard the runtime; the second discards a working one and
            # the turn dies with it. The caller is what separates them.
            asker = "<unknown>"
            frame = sys._getframe(1)
            for _ in range(4):
                if frame is None:
                    break
                name = frame.f_code.co_name
                if name not in ("get_runtime", "ensure_runtime"):
                    asker = f"{Path(frame.f_code.co_filename).name}:{name}"
                    break
                frame = frame.f_back
            logger.warning(
                "ignore stale runtime session=%s runtime_sandbox=%s "
                "expected_sandbox=%s asked_by=%s",
                session_id,
                runtime_sandbox_id or "<missing>",
                expected_sandbox_id or "<none>",
                asker,
            )
            return None
        unusable_reason = self._runtime_client_unusable_reason(runtime)
        if unusable_reason is not None:
            # Pop, don't just ignore: a dead-link runtime left in the map
            # would shadow the fresh one the caller is about to attach. There
            # is nothing async to tear down, because a dead link is exactly
            # what makes the runtime unusable here.
            if self._runtimes.get(session_id) is runtime:
                self._runtimes.pop(session_id, None)
            logger.warning(
                "evict unusable runtime session=%s sandbox=%s reason=%s",
                session_id,
                self._runtime_sandbox_id(runtime) or "<unknown>",
                unusable_reason,
            )
            return None
        return runtime

    def resolve_session_terminal_cwd(
        self,
        session_id: str,
        *,
        sandbox_id: Any = _RUNTIME_SANDBOX_BINDING_UNSPECIFIED,
        session_kind: str,
        engine_session_key: str | None = None,
    ) -> str | None:
        runtime = self.get_runtime(session_id, sandbox_id=sandbox_id)
        runtime_cwd = (
            str(getattr(runtime, "terminal_cwd", "") or "").strip() if runtime is not None else ""
        )
        if runtime_cwd:
            return runtime_cwd

        planner = self._workspace_planner()
        return planner.resolve_terminal_cwd(
            session_id=session_id,
            session_kind=session_kind,
            engine_session_key=engine_session_key,
            existing_terminal_cwd=None,
        )

    def _workspace_planner(self) -> SessionWorkspacePlanner:
        return SessionWorkspacePlanner(str(self._resolve_remote_cwd() or "").strip())

    def plan_agent_chat_runtime_start(
        self,
        *,
        session_id: str,
        agent_id: str,
        template: AgentView,
        resume_engine_session_key: str | None,
        existing_terminal_cwd: str | None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> RuntimeWorkspacePlan:
        return self._workspace_planner().plan_agent_chat_runtime_start(
            session_id=session_id,
            agent_id=agent_id,
            template=template,
            resume_engine_session_key=resume_engine_session_key,
            existing_terminal_cwd=existing_terminal_cwd,
            runtime_identity=runtime_identity,
        )

    def plan_runtime_attach(
        self,
        *,
        agent_id: str,
        session_id: str,
        session_kind: str,
        sandbox_id: str,
        engine_session_key: str | None,
        existing_terminal_cwd: str | None,
        engine_kind: str,
        runtime_identity: dict[str, Any] | None = None,
    ) -> RuntimeWorkspacePlan:
        return self._workspace_planner().plan_runtime_attach(
            agent_id=agent_id,
            session_id=session_id,
            session_kind=session_kind,
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            existing_terminal_cwd=existing_terminal_cwd,
            engine_kind=engine_kind,
            runtime_identity=runtime_identity,
        )

    def plan_agent_runtime_start(
        self,
        *,
        agent_id: str,
        runtime_key: str,
        template: AgentView,
    ) -> RuntimeWorkspacePlan:
        return self._workspace_planner().plan_agent_runtime_start(
            agent_id=agent_id,
            runtime_key=runtime_key,
            template=template,
        )

    def plan_assistant_runtime_start(
        self,
        *,
        user_id: str,
        assistant_id: str,
        runtime_key: str,
        template: AgentView,
        engine_kind: str,
    ) -> RuntimeWorkspacePlan:
        """Build a plan for an assistant-bound runtime.

        ``engine_kind`` originates from the ``assistant_catalog`` row's
        immutable-after-materialize field; it is carried into ``RuntimeWorkspacePlan``
        so the engine dispatcher (``_start_runtime`` → ``get_engine_adapter``)
        can route to any registered adapter without the lifecycle layer knowing
        the engine ABI. Runtime-subject and session-kind constraints are enforced
        by ``RuntimeWorkspacePlan.__post_init__``.
        """
        return self._workspace_planner().plan_assistant_runtime_start(
            user_id=user_id,
            assistant_id=assistant_id,
            runtime_key=runtime_key,
            template=template,
            engine_kind=engine_kind,
        )

    def plan_assistant_runtime_attach(
        self,
        *,
        user_id: str,
        assistant_id: str,
        runtime_key: str,
        sandbox_id: str,
        existing_terminal_cwd: str | None,
        engine_kind: str,
    ) -> RuntimeWorkspacePlan:
        return self._workspace_planner().plan_assistant_runtime_attach(
            user_id=user_id,
            assistant_id=assistant_id,
            runtime_key=runtime_key,
            sandbox_id=sandbox_id,
            existing_terminal_cwd=existing_terminal_cwd,
            engine_kind=engine_kind,
        )

    def plan_agent_conversation_root(
        self,
        *,
        session_id: str,
        sandbox_id: str,
        template: Any,
        materialization_pending: bool,
        runtime_identity: dict[str, Any] | None = None,
    ) -> ConversationWorkspacePlan:
        return self._workspace_planner().plan_agent_conversation_root(
            session_id=session_id,
            sandbox_id=sandbox_id,
            template=template,
            materialization_pending=materialization_pending,
            runtime_identity=runtime_identity,
        )

    # ── Config delegation ───

    def resolve_template_model_name(self, template: AgentView) -> str | None:
        return self._config.resolve_template_model_name(template)

    def _resolve_model_access(self, mc: dict[str, Any]) -> ResolvedModelAccess:
        return self._config.resolve_model_access(mc)

    def _resolve_sandbox_api_key(self) -> str | None:
        return self._config.resolve_sandbox_api_key()

    def _resolve_sandbox_backend_secret(
        self,
        template: Any,
        *,
        backend: str | None = None,
    ) -> str:
        provider = (
            sandbox_for_name(backend)
            if str(backend or "").strip()
            else sandbox_for_template(template)
        )
        return provider.secret_material(settings=self._settings)

    def _build_sandbox_connection_config(
        self, template: Any, connection_config_cls: Any
    ) -> Any | None:
        adapter = sandbox_for_template(template)
        secret_material = None
        if adapter.connection_secret_uses_legacy_sandbox_api_key:
            secret_material = self._resolve_sandbox_api_key()
        return adapter.connection_config(
            connection_config_cls=connection_config_cls,
            settings=self._settings,
            request_timeout_seconds=self._settings.sandbox_request_timeout_seconds,
            secret_material=secret_material,
        )

    def _resolve_remote_cwd(self) -> str | None:
        return self._config.resolve_remote_cwd()

    # Module-level helpers re-exposed on the class, so a caller holding only a
    # manager instance can reach them.
    _resolve_network_policy = staticmethod(resolve_network_policy)
    _resolve_runtime_template_name = staticmethod(resolve_runtime_template_name)
    _is_truthy = staticmethod(is_truthy)
    _is_local_mode = staticmethod(is_local_mode)
    _get_underlying_sandbox = staticmethod(get_underlying_sandbox)
    _extract_sandbox_id = staticmethod(extract_sandbox_id)
    _extract_command_log_text = staticmethod(extract_command_log_text)
    _normalize_diag_text = staticmethod(normalize_diag_text)
    _is_initialize_timeout_error = staticmethod(is_initialize_timeout_error)
    _is_claude_server_start_timeout_error = staticmethod(is_claude_server_start_timeout_error)
    _build_runtime_start_error_message = staticmethod(build_runtime_start_error_message)

    # ── Sandbox connection (delegate to sandbox_client) ────────────

    async def _resolve_sandbox_backend(self, sandbox_id: str) -> str:
        """Resolve a sandbox's persisted backend by id (persisted source of truth).

        Looks up the row that owns the sandbox id. Normal chat/assistant boxes are
        owned by a session row; shared Agent runtime boxes are owned by an agent row.
        Dispatch then routes by this persisted backend — never by guessing from the
        id string. Fails loud if unresolvable: there is no privileged default backend.
        Cached, since the backend is immutable per sandbox id.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            raise RuntimeError("cannot resolve sandbox backend: empty sandbox_id")
        cached = self._sandbox_backend_cache.get(target)
        if cached:
            return cached
        session = await self._sessions_repo.find_session_by_sandbox_id(target)
        startup_allocation = (
            (session or {}).get("startup_allocation")
            if isinstance((session or {}).get("startup_allocation"), dict)
            else {}
        )
        startup_backend = (
            startup_allocation.get("sandbox_backend")
            if str(startup_allocation.get("sandbox_id") or "").strip() == target
            else None
        )
        backend = str(
            startup_backend or (session or {}).get("sandbox_backend") or ""
        ).strip().lower()
        if backend:
            self._sandbox_backend_cache[target] = backend
            return backend
        agent = await AgentRepository().find_agent_by_sandbox_id(target)
        agent_backend = str((agent or {}).get("sandbox_backend") or "").strip().lower()
        if agent_backend:
            self._sandbox_backend_cache[target] = agent_backend
            return agent_backend
        if not backend:
            owner_detail = "no session or agent row owns it"
            if session is not None and agent is not None:
                owner_detail = "owning session and agent rows have no sandbox_backend"
            elif session is not None:
                owner_detail = "owning session row has no sandbox_backend"
            elif agent is not None:
                owner_detail = "owning agent row has no sandbox_backend"
            raise RuntimeError(
                f"cannot resolve sandbox backend for sandbox_id={target!r}: " + owner_detail
            )

    async def connect_sandbox_only(self, sandbox_id: str) -> Any:
        backend = await self._resolve_sandbox_backend(sandbox_id)
        # Deadline the unary connect so a wedged dockerd surfaces as a connect
        # failure instead of hanging the caller (never the stream).
        async with asyncio.timeout(_sandbox_control_deadline_s()):
            return await sandbox_for_name(backend).connect(sandbox_id)

    async def destroy_sandbox_by_id(self, sandbox_id: str) -> SandboxDestruction:
        """Destroy one sandbox by id and report the evidence, never a bare bool.

        A row naming the id selects the backend but does not grant permission;
        :meth:`SandboxProvider.confirm_destroyed` supplies the destruction
        evidence. Backend resolution also consults the create-time cache so a
        deleted owner row does not make its sandbox unreachable. An unresolved
        backend returns a refused verdict, preserving the id for retry.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return SandboxDestruction.nothing_named(
                detail="destroy_sandbox_by_id was called with no sandbox id"
            )
        try:
            backend = await self._resolve_sandbox_backend(target)
        except Exception as exc:  # noqa: BLE001 - an unresolvable backend is a refusal
            return SandboxDestruction.refused(
                target, detail=f"the sandbox's backend could not be resolved: {exc}"
            )
        try:
            provider = sandbox_for_name(backend)
        except Exception as exc:  # noqa: BLE001 - an unregistered backend, likewise
            return SandboxDestruction.refused(
                target,
                detail=f"backend {backend!r} is not registered in this process: {exc}",
            )
        deadline = _sandbox_control_deadline_s()
        try:
            # Deadline the destroy so a wedged control plane surfaces as an
            # unconfirmed destruction instead of hanging startup cleanup.
            # Reclaim snapshots before the destroy, and only in this order:
            # deleting a sandbox drops its snapshot records with it, so once the
            # box is gone there is no id left to ask about, while the images
            # those records named stay in the registry. One orphaned image per
            # pause adds up; together with build cache it fills the node's disk,
            # after which kubelet reports DiskPressure and every create fails
            # with a network error three layers from the cause.
            #
            # Best-effort by contract: a snapshot that cannot be reclaimed must never
            # keep a box alive. Leaking an image costs disk; leaking a running box
            # costs money and privacy, so the destroy always proceeds.
            discard = getattr(provider, "discard_snapshots", None)
            if callable(discard):
                with contextlib.suppress(Exception):
                    await discard(target)
            async with asyncio.timeout(deadline):
                return await provider.confirm_destroyed(target)
        except TimeoutError:
            return SandboxDestruction.unconfirmed(
                target,
                detail=f"the destroy did not answer within {deadline:g}s",
            )
        except Exception as exc:  # noqa: BLE001 - a raise proves nothing either way
            return SandboxDestruction.unconfirmed(
                target, detail=f"the destroy raised {type(exc).__name__}: {exc}"
            )

    async def get_sandbox_expires_at(self, sandbox_id: str) -> datetime | None:
        backend = await self._resolve_sandbox_backend(sandbox_id)
        return await sandbox_for_name(backend).expires_at(sandbox_id)

    async def get_sandbox_lifecycle_probe(self, sandbox_id: str) -> SandboxLifecycleProbeResult:
        try:
            backend = await self._resolve_sandbox_backend(sandbox_id)
        except Exception as exc:
            # Per-turn hot path: return probe-failed instead of raising, so a
            # row whose backend cannot be resolved is not treated as confirmed dead.
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
                error_text=f"backend unresolved: {exc}"[:200],
            )
        deadline = _sandbox_control_deadline_s()
        try:
            # Deadline the unary probe: a wedged dockerd must surface as a transient
            # PROBE_FAILED, never hang the watcher/turn caller. The empty sandbox_state
            # keeps it non-terminal, so a timed-out probe never converges a live
            # binding — a wedged control plane is not evidence the box is gone.
            async with asyncio.timeout(deadline):
                return await sandbox_for_name(backend).probe(sandbox_id)
        except TimeoutError:
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
                error_text=f"lifecycle probe timed out after {deadline:g}s",
            )

    async def renew_runtime(self, session_id: str, ttl_seconds: float) -> datetime | None:
        rt = self._runtimes.get(session_id)
        sandbox_id = str(getattr(rt, "sandbox_id", "") or "").strip() if rt else ""
        if not sandbox_id:
            return None
        return await self.renew_sandbox_by_id(sandbox_id, int(ttl_seconds))

    async def maybe_renew_lease_on_activity(self, session_id: str) -> None:
        """Lazily renew a borrowed sandbox's lease on turn activity.

        Only the owner replica (the one holding this session's resident runtime)
        renews, and only when the remaining lease drops below the threshold — so an
        active session keeps its sandbox while an abandoned one stops being renewed
        and auto-expires ~one lease after its last turn (leaks are prevented by the
        native TTL, not a reaper). renew sets expires_at to now+lease (absolute), so
        this is multi-machine-safe even if it races another renew path. Best-effort:
        a renew failure never breaks the turn.
        """
        rt = self._runtimes.get(session_id)
        if rt is None or not str(getattr(rt, "sandbox_id", "") or "").strip():
            return
        lease = int(getattr(self._settings, "sandbox_lease_seconds", 14400))
        threshold = int(getattr(self._settings, "sandbox_lease_renew_threshold_seconds", 3600))
        exp = getattr(rt, "sandbox_lease_expires_at", None)
        if isinstance(exp, datetime):
            now = datetime.now(timezone.utc)
            exp_utc = exp if exp.tzinfo is not None else exp.replace(tzinfo=timezone.utc)
            if (exp_utc - now).total_seconds() > threshold:
                return  # plenty of lease left — throttle, don't renew every turn
        try:
            new_exp = await self.renew_runtime(
                session_id, await self._activity_lease_seconds(session_id, lease)
            )
        except Exception as exc:
            logger.warning("sandbox lease renew failed session=%s: %s", session_id, exc)
            return
        rt.sandbox_lease_expires_at = new_exp
        # Manual cleanup clears the cached finite expiry as well.
        with contextlib.suppress(Exception):
            await SessionRepository().compare_and_update_session(
                session_id,
                expected={"sandbox_id": rt.sandbox_id},
                updates={"expires_at": new_exp.isoformat() if new_exp is not None else None},
            )

    async def _activity_lease_seconds(self, session_id: str, lease: int) -> int:
        """The lease a box is renewed to on activity, by what the box is for.

        A conversation's box is leased for ``sandbox_lease_seconds`` and let go
        one lease after its last turn. An Assistant's workspace box is created
        with the longer ``agent_sandbox_renew_ttl_seconds`` horizon, and the
        provider's renew never shortens a lease, so writing the conversation
        lease over it would do nothing until the horizon had run down to it —
        and from then on end the workspace hours after the day's last message
        instead of days later. Read after the threshold check, so an active
        session costs one row read per renewal, not per turn.
        """
        session = await self._sessions_repo.get_session(session_id)
        kind = str((session or {}).get("session_kind") or "").strip()
        if kind != "assistant_chat":
            return lease
        horizon = int(getattr(self._settings, "agent_sandbox_renew_ttl_seconds", 0) or 0)
        return max(lease, horizon)

    async def renew_sandbox_by_id(self, sandbox_id: str, ttl_seconds: int) -> datetime | None:
        backend = await self._resolve_sandbox_backend(sandbox_id)
        return await sandbox_for_name(backend).renew(sandbox_id, ttl_seconds)

    async def resolve_sandbox_provider(self, sandbox_id: str) -> Any:
        """The provider that owns this box, for asking what it can do.

        A capability has to be checked against the backend the box is on, not
        the deployment default: an environment selects its own
        ``sandbox_backend``, so a caller that gated on the default would refuse
        an operation the box supports, or attempt one it does not. Returning the
        provider rather than a bool keeps ``name`` available, which is what lets
        a refusal say which backend cannot do the thing.
        """
        backend = await self._resolve_sandbox_backend(sandbox_id)
        return sandbox_for_name(backend)

    async def pause_sandbox_by_id(self, sandbox_id: str) -> bool:
        """Park a box: commit its filesystem, free its compute. True when PAUSED.

        The lease is not extended here. A paused box still expires on its lease and
        takes its snapshot with it, so whoever parks a box owns the retention
        decision and must have made it before this call: renewing a paused sandbox
        makes the control plane fail it outright.
        """
        backend = await self._resolve_sandbox_backend(sandbox_id)
        return await sandbox_for_name(backend).pause(sandbox_id)

    async def resume_sandbox_by_id(self, sandbox_id: str) -> bool:
        """Restore a parked box under the same id. True when it is RUNNING again.

        A resume is a fresh boot on old files: the image's entrypoint runs again and
        the in-box control server comes back with it. Nothing in the box is carried
        over from before the pause, so a caller must re-establish its connection
        rather than reuse one.
        """
        backend = await self._resolve_sandbox_backend(sandbox_id)
        return await sandbox_for_name(backend).resume(sandbox_id)

    @staticmethod
    def _normalize_sandbox_id(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @classmethod
    def _sandbox_ids_conflict(cls, left: Any, right: Any) -> bool:
        left_id = cls._normalize_sandbox_id(left)
        right_id = cls._normalize_sandbox_id(right)
        return bool(left_id and right_id and left_id.lower() != right_id.lower())

    @classmethod
    def _runtime_sandbox_id(cls, runtime: SessionRuntime | None) -> str | None:
        if runtime is None:
            return None
        runtime_sandbox_id = cls._normalize_sandbox_id(getattr(runtime, "sandbox_id", None))
        if runtime_sandbox_id:
            return runtime_sandbox_id
        sandbox_obj = getattr(runtime, "sandbox", None)
        return cls._normalize_sandbox_id(
            extract_sandbox_id(sandbox_obj)
        ) or cls._normalize_sandbox_id(extract_sandbox_id(get_underlying_sandbox(sandbox_obj)))

    @classmethod
    def _runtime_sandbox_mismatch(
        cls,
        runtime: SessionRuntime | None,
        expected_sandbox_id: Any,
    ) -> tuple[str | None, str | None] | None:
        if runtime is None:
            return None
        if expected_sandbox_id is _RUNTIME_SANDBOX_BINDING_UNSPECIFIED:
            return None
        expected_id = cls._normalize_sandbox_id(expected_sandbox_id)
        runtime_id = cls._runtime_sandbox_id(runtime)
        if not expected_id:
            return (runtime_id, None) if runtime_id else None
        if runtime_id and runtime_id.lower() == expected_id.lower():
            return None
        return runtime_id, expected_id

    @staticmethod
    def _task_done(task: Any) -> bool:
        done = getattr(task, "done", None)
        if not callable(done):
            return False
        try:
            return bool(done())
        except Exception:
            return False

    @classmethod
    def _runtime_client_unusable_reason(cls, runtime: SessionRuntime | None) -> str | None:
        if runtime is None:
            return None
        engine_client = getattr(runtime, "engine_client", None)
        if engine_client is None:
            return "missing_engine_client"
        # The client's own death certificate (EngineClient.is_live): a runner
        # link whose peer went away must not be handed another turn. The
        # property is mandatory, so absence or an indeterminate value is a
        # broken runtime rather than evidence that the link is healthy.
        is_live = getattr(engine_client, "is_live", None)
        if is_live is False:
            return "engine_link_dead"
        if is_live is not True:
            return "engine_liveness_contract_invalid"
        return None

    @staticmethod
    async def _disconnect_evicted_runtime(
        runtime: SessionRuntime,
        *,
        session_id: str,
        reason: str,
    ) -> None:
        with contextlib.suppress(BaseException):
            if runtime.current_task and not runtime.current_task.done():
                runtime.current_task.cancel()
        with contextlib.suppress(Exception):
            await RemoteAgentRuntimeManager._close_runtime_turn_client(runtime)
        logger.info("evicted runtime session=%s reason=%s", session_id, reason)

    async def _drop_runtime_for_sandbox_mismatch(
        self,
        session_id: str,
        runtime: SessionRuntime | None,
        expected_sandbox_id: Any,
        *,
        reason: str,
    ) -> SessionRuntime | None:
        mismatch = self._runtime_sandbox_mismatch(runtime, expected_sandbox_id)
        if mismatch is None or runtime is None:
            return runtime
        runtime_sandbox_id, expected_id = mismatch
        if self._runtimes.get(session_id) is runtime:
            self._runtimes.pop(session_id, None)
        logger.warning(
            "evict stale runtime session=%s runtime_sandbox=%s expected_sandbox=%s reason=%s",
            session_id,
            runtime_sandbox_id or "<missing>",
            expected_id or "<none>",
            reason,
        )
        await self._disconnect_evicted_runtime(runtime, session_id=session_id, reason=reason)
        return None

    async def _drop_runtime_for_unusable_client(
        self,
        session_id: str,
        runtime: SessionRuntime | None,
        *,
        reason: str,
    ) -> SessionRuntime | None:
        unusable_reason = self._runtime_client_unusable_reason(runtime)
        if unusable_reason is None or runtime is None:
            return runtime
        if self._runtimes.get(session_id) is runtime:
            self._runtimes.pop(session_id, None)
        logger.warning(
            "evict unusable runtime session=%s sandbox=%s client_reason=%s reason=%s",
            session_id,
            self._runtime_sandbox_id(runtime) or "<unknown>",
            unusable_reason,
            reason,
        )
        await self._disconnect_evicted_runtime(runtime, session_id=session_id, reason=reason)
        return None

    async def resolve_enhanced_server_endpoint(
        self, *, session_id: str | None = None, sandbox_id: str | None = None, port: int = 8000
    ) -> str | None:
        requested_sandbox_id = self._normalize_sandbox_id(sandbox_id)
        rt = self._runtimes.get(session_id) if session_id else None
        if session_id:
            rt = await self._drop_runtime_for_sandbox_mismatch(
                session_id,
                rt,
                requested_sandbox_id or _RUNTIME_SANDBOX_BINDING_UNSPECIFIED,
                reason="resolve_enhanced_server_endpoint",
            )
        runtime_sandbox_id = self._runtime_sandbox_id(rt)
        sandbox_obj = None
        if rt is not None:
            sandbox_obj = rt.sandbox
        eff_id = requested_sandbox_id or runtime_sandbox_id
        return await resolve_enhanced_server_endpoint(
            sandbox_obj, sandbox_id=eff_id or None, port=port, connect_fn=self.connect_sandbox_only
        )

    async def resolve_sandbox_endpoint_by_id(
        self, sandbox_id: str, *, port: int = 8000
    ) -> str | None:
        backend = await self._resolve_sandbox_backend(sandbox_id)
        return await sandbox_for_name(backend).resolve_endpoint(sandbox_id, port)

    async def _check_server_health(self, sandbox, port: int, timeout: float = 5.0) -> bool:
        return await check_server_health(sandbox, port, timeout)

    async def _reset_server_session(
        self, sandbox, port: int, session_id: str, timeout: float = 5.0
    ) -> None:
        return await reset_server_session(sandbox, port, session_id, timeout)

    @staticmethod
    def _extract_sandbox_id_from_endpoint(endpoint: str | None) -> str | None:
        value = str(endpoint or "").strip()
        for pattern in _SANDBOX_ID_IN_ENDPOINT_RES:
            match = pattern.search(value)
            if match:
                return match.group(1).lower()
        return None

    async def _resolve_validated_sandbox_endpoint(
        self,
        *,
        session_id: str | None = None,
        sandbox_endpoint: str | None = None,
        sandbox_id: str | None = None,
        port: int = 8000,
        reason: str = "sandbox_endpoint",
    ) -> str | None:
        endpoint = str(sandbox_endpoint or "").strip()
        requested_sandbox_id = self._normalize_sandbox_id(sandbox_id)
        # Some backends' presign endpoints are TTL-bound full URLs; prefer re-minting via
        # sandbox_id when available.  Other backends' endpoints are also full URLs
        # but are themselves the authoritative data-plane address — clearing them
        # forces an unnecessary resolve round-trip (30s timeout).  Keep the URL when
        # the sandbox backend provides authoritative endpoints.
        if endpoint.startswith(("http://", "https://")) and requested_sandbox_id:
            rt = self._runtimes.get(session_id) if session_id else None
            backend = (
                sandbox_for_sandbox(rt.sandbox) if rt and getattr(rt, "sandbox", None) else None
            )
            # Resolve the backend from the live sandbox via the seam; if there is none, leave it
            # unresolved — a pluggable framework must not guess which backend owns an id. The
            # ``getattr`` below then treats an unresolved backend as not-authoritative.
            if not getattr(backend, "endpoint_is_authoritative", False):
                endpoint = ""
        endpoint_sandbox_id = self._extract_sandbox_id_from_endpoint(endpoint)
        runtime = self._runtimes.get(session_id) if session_id else None
        expected_sandbox_id = requested_sandbox_id or endpoint_sandbox_id

        if session_id and expected_sandbox_id:
            runtime = await self._drop_runtime_for_sandbox_mismatch(
                session_id,
                runtime,
                expected_sandbox_id,
                reason=reason,
            )
        runtime_sandbox_id = self._runtime_sandbox_id(runtime)
        expected_sandbox_id = requested_sandbox_id or endpoint_sandbox_id or runtime_sandbox_id

        if endpoint and expected_sandbox_id:
            if endpoint_sandbox_id:
                if endpoint_sandbox_id.lower() != expected_sandbox_id.lower():
                    logger.warning(
                        "discard stale sandbox endpoint session=%s endpoint_sandbox=%s expected_sandbox=%s reason=%s endpoint=%s",
                        session_id or "<none>",
                        endpoint_sandbox_id,
                        expected_sandbox_id,
                        reason,
                        endpoint,
                    )
                    endpoint = ""
            else:
                logger.warning(
                    "discard unverifiable sandbox endpoint session=%s expected_sandbox=%s reason=%s endpoint=%s",
                    session_id or "<none>",
                    expected_sandbox_id,
                    reason,
                    endpoint,
                )
                endpoint = ""

        if endpoint:
            return endpoint

        if expected_sandbox_id:
            return (
                await self.resolve_enhanced_server_endpoint(
                    session_id=session_id,
                    sandbox_id=expected_sandbox_id,
                    port=port,
                )
                or ""
            ).strip() or None
        if session_id:
            return (
                await self.resolve_enhanced_server_endpoint(
                    session_id=session_id,
                    port=port,
                )
                or ""
            ).strip() or None
        return None

    async def resolve_sandbox_dataplane(
        self,
        *,
        session_id: str | None = None,
        sandbox_endpoint: str | None = None,
        sandbox_id: str | None = None,
        port: int = 8000,
        reason: str = "sandbox_dataplane",
        require_ws: bool = False,
        persisted_backend: str | None = None,
    ) -> SandboxDataPlane | None:
        """Resolve a backend-agnostic ``SandboxDataPlane`` for in-box HTTP/ws.

        The single chokepoint the platform uses instead of building
        ``https://{host}{path}`` + ``httpx`` by hand. It pairs the validated
        endpoint string (a bare host for some backends, a presign URL for gateway backends)
        with the in-memory sandbox object (when present — needed by gateway
        backends for header-authed HTTP and ws routing) and lets the dataplane
        registry pick the implementation. Returns ``None`` only when neither an
        endpoint nor a sandbox object can be resolved.
        """
        authoritative_backend = str(persisted_backend or "").strip().lower()
        authoritative_provider = (
            sandbox_for_name(authoritative_backend) if authoritative_backend else None
        )
        endpoint = await self._resolve_validated_sandbox_endpoint(
            session_id=session_id,
            sandbox_endpoint=sandbox_endpoint,
            sandbox_id=sandbox_id,
            port=port,
            reason=reason,
        )
        sandbox_obj = None
        runtime = self._runtimes.get(session_id) if session_id else None
        if runtime is not None:
            sandbox_obj = get_underlying_sandbox(getattr(runtime, "sandbox", None))
        target_sandbox_id = self._normalize_sandbox_id(sandbox_id)
        if sandbox_obj is None and require_ws and target_sandbox_id:
            ws_provider = authoritative_provider
            if ws_provider is None:
                backend = await self._resolve_sandbox_backend(target_sandbox_id)
                ws_provider = sandbox_for_name(backend)
            if ws_provider.requires_sandbox_object_for_ws:
                sandbox_obj = await ws_provider.connect(target_sandbox_id)
        if not endpoint and sandbox_obj is None:
            return None
        if sandbox_obj is not None:
            # Live object in scope: its owning provider builds the transport
            # (and carries whatever the ws path needs).
            if authoritative_provider is not None:
                if not authoritative_provider.owns_sandbox(sandbox_obj):
                    raise RuntimeError(
                        "persisted sandbox backend does not own the live sandbox "
                        f"object: backend={authoritative_backend!r} "
                        f"sandbox_id={target_sandbox_id!r}"
                    )
                return authoritative_provider.build_dataplane(
                    sandbox=sandbox_obj, endpoint=endpoint, port=port
                )
            return sandbox_for_sandbox(sandbox_obj).build_dataplane(
                sandbox=sandbox_obj, endpoint=endpoint, port=port
            )
        # Endpoint-only reach: no live object, so the backend name must be
        # threaded explicitly (no registry to guess from). Resolve it from the
        # sandbox id the endpoint was validated against — or, failing that, the
        # session row that owns this conversation. There is no default backend.
        resolved_backend = authoritative_backend or await self._resolve_dataplane_backend(
            session_id=session_id,
            sandbox_id=target_sandbox_id,
            endpoint=endpoint,
        )
        return sandbox_for_name(resolved_backend).build_dataplane(endpoint=endpoint, port=port)

    async def _resolve_dataplane_backend(
        self,
        *,
        session_id: str | None,
        sandbox_id: str | None,
        endpoint: str | None,
    ) -> str:
        """Resolve the backend name for an endpoint-only dataplane build, fail loud.

        Tries, in order: the explicit/normalized sandbox id, the id embedded in the
        endpoint, then the owning session row's persisted ``sandbox_backend``.
        Raises if none yields a backend — a pluggable framework never guesses.
        """
        for candidate in (
            self._normalize_sandbox_id(sandbox_id),
            self._extract_sandbox_id_from_endpoint(endpoint),
        ):
            if candidate:
                return await self._resolve_sandbox_backend(candidate)
        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id:
            session = await SessionRepository().get_session(normalized_session_id)
            backend = str((session or {}).get("sandbox_backend") or "").strip().lower()
            if backend:
                return backend
        raise RuntimeError(
            "cannot resolve sandbox backend for endpoint-only dataplane: "
            f"session_id={session_id!r} sandbox_id={sandbox_id!r} endpoint={endpoint!r}"
        )

    @staticmethod
    def _is_terminal_interaction_broker_sandbox_state(state: str | None) -> bool:
        return str(state or "").strip().lower() in _INTERACTION_BROKER_TERMINAL_SANDBOX_STATES

    def _is_terminal_turn_sandbox_lifecycle_probe(
        self,
        probe: SandboxLifecycleProbeResult,
    ) -> bool:
        """Whether a lifecycle probe proves that turn execution has ended."""
        if (
            str(getattr(probe, "probe_status", "") or "").strip()
            == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND
        ):
            return True
        return self._is_terminal_interaction_broker_sandbox_state(
            getattr(probe, "sandbox_state", None)
        )

    @staticmethod
    def _sandbox_uses_create_storage_mounts(sandbox: Any) -> bool:
        adapter = sandbox_for_sandbox(get_underlying_sandbox(sandbox))
        return bool(adapter and adapter.uses_create_oss_mounts)

    async def _mount_assistant_workspace_storage(
        self,
        sandbox: Any,
        *,
        user_id: str,
        assistant_id: str,
        engine_kind: str,
    ) -> None:
        # Backends with create-time storage mounts already mounted the shared
        # assistant root; the runtime NFS mount path does not apply.
        if self._sandbox_uses_create_storage_mounts(sandbox):
            return
        # Backends whose shared assistant root is baked into the image / bound at
        # create likewise have no runtime NAS mount to perform.
        adapter = sandbox_for_sandbox(get_underlying_sandbox(sandbox))
        if adapter and adapter.assistant_workspace_root_preprovisioned:
            return
        await mount_assistant_workspace_storage(
            sandbox,
            user_id=user_id,
            assistant_id=assistant_id,
            engine_kind=engine_kind,
            settings=self._settings,
            get_underlying_sandbox_fn=get_underlying_sandbox,
        )

    async def _clone_default_repo(
        self,
        sandbox: Any,
        template: AgentView,
        target_cwd: str,
        session_id: str,
        *,
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        await clone_default_repo(
            sandbox,
            template,
            target_cwd,
            session_id,
            get_underlying_sandbox_fn=get_underlying_sandbox,
            runtime_identity=runtime_identity,
        )

    @staticmethod
    def _validate_runtime_start_plan(session_id: str, workspace_plan: RuntimeWorkspacePlan) -> None:
        if workspace_plan.operation != "runtime_start":
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message=f"runtime start requires runtime_start plan, got {workspace_plan.operation}",
                status_code=500,
            )
        if workspace_plan.runtime_key != session_id:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message=(
                    "runtime start plan key does not match requested runtime: "
                    f"plan={workspace_plan.runtime_key} requested={session_id}"
                ),
                status_code=500,
            )
        if not str(workspace_plan.cwd or "").strip():
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="runtime start plan cwd is empty",
                status_code=500,
            )

    @staticmethod
    def _validate_runtime_attach_plan(
        session_id: str, sandbox_id: str, workspace_plan: RuntimeWorkspacePlan
    ) -> None:
        if workspace_plan.operation != "runtime_attach":
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message=f"runtime attach requires runtime_attach plan, got {workspace_plan.operation}",
                status_code=500,
            )
        if workspace_plan.runtime_key != session_id:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message=(
                    "runtime attach plan key does not match requested runtime: "
                    f"plan={workspace_plan.runtime_key} requested={session_id}"
                ),
                status_code=500,
            )
        if str(workspace_plan.sandbox_id or "").strip() != str(sandbox_id or "").strip():
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message=(
                    "runtime attach plan sandbox does not match requested sandbox: "
                    f"plan={workspace_plan.sandbox_id} requested={sandbox_id}"
                ),
                status_code=500,
            )
        if workspace_plan.materialize_default_repo:
            raise APIError(
                code="WORKSPACE_PLAN_INVALID",
                message="runtime attach plan cannot materialize default_repo",
                status_code=500,
            )

    # ── Runtime lifecycle ──────────────────────────────────────────

    async def create_runtime(
        self,
        session_id: str,
        template: AgentView,
        *,
        assignment_id: str,
        user_id: str | None = None,
        permission_mode: str | None = None,
        progress_callback: ProgressCallback = None,
        callback_url: str | None = None,
        workspace_plan: RuntimeWorkspacePlan,
        startup_guard: Callable[[], Awaitable[Any]] | None = None,
    ) -> SessionRuntime:
        self._raise_if_quiesced()
        if startup_guard is not None:
            await startup_guard()
        if not str(assignment_id or "").strip():
            raise APIError(
                code="SANDBOX_ASSIGNMENT_INVALID",
                message="runtime creation requires a durable assignment_id",
                status_code=500,
            )
        self._validate_runtime_start_plan(session_id, workspace_plan)
        existing = self._runtimes.get(session_id)
        existing = await self._drop_runtime_for_unusable_client(
            session_id,
            existing,
            reason="create_runtime",
        )
        if existing is not None:
            return existing
        async with self._get_session_lock(session_id):
            self._raise_if_quiesced()
            if startup_guard is not None:
                await startup_guard()
            existing = self._runtimes.get(session_id)
            existing = await self._drop_runtime_for_unusable_client(
                session_id,
                existing,
                reason="create_runtime",
            )
            if existing is not None:
                return existing
            runtime = await self._start_runtime(
                session_id=session_id,
                template=template,
                assignment_id=assignment_id,
                user_id=user_id,
                permission_mode=permission_mode,
                progress_callback=progress_callback,
                callback_url=callback_url,
                workspace_plan=workspace_plan,
            )
            try:
                self._raise_if_quiesced()
                self._require_runtime_engine_manifest(runtime)
            except APIError as exc:
                if self._runtimes.get(session_id) is runtime:
                    self._runtimes.pop(session_id, None)
                await self._disconnect_runtime_client(
                    runtime,
                    session_id=session_id,
                    reason=self._quiesced_reason or "runtime publication failed",
                )
                if self._quiesced_reason:
                    raise
                leaked_sandbox_id, cleanup_error = await self._rollback_runtime_start(
                    session_id
                )
                wrapped = self._runtime_start_error(
                    engine_kind=workspace_plan.engine_kind,
                    original=exc,
                    leaked_sandbox_id=leaked_sandbox_id,
                    cleanup_error=cleanup_error,
                )
                raise wrapped from exc
            if startup_guard is not None:
                try:
                    await startup_guard()
                except BaseException:
                    # This exact provisional client is local to the lost
                    # startup. Its allocation remains durably named for the
                    # existing reconciler; it may not destroy a successor box.
                    await self._disconnect_runtime_client(
                        runtime, session_id=session_id, reason="startup owner changed",
                    )
                    raise
            setattr(runtime, "permission_mode_verified", True)
            runtime.user_id = str(user_id or "").strip() or None
            self._runtimes[session_id] = runtime
            # Process-local registration is not durable publication. Keep the
            # startup allocation until the lifecycle worker writes READY.
            return runtime

    async def ensure_runtime(
        self,
        session_id: str,
        template: AgentView,
        *,
        sandbox_id: Any = _RUNTIME_SANDBOX_BINDING_UNSPECIFIED,
        assignment_id: str | None = None,
        user_id: str | None = None,
        engine_session_key: str | None = None,
        permission_mode: str | None = None,
        progress_callback: ProgressCallback = None,
        session_kind: str,
        callback_url: str | None = None,
        workspace_plan: RuntimeWorkspacePlan | None = None,
        runtime_identity: dict[str, Any] | None = None,
        startup_guard: Callable[[], Awaitable[Any]] | None = None,
    ) -> SessionRuntime:
        self._raise_if_quiesced()
        if startup_guard is not None:
            await startup_guard()
        existing = self.get_runtime(session_id, sandbox_id=sandbox_id)
        if existing is not None:
            return existing
        async with self._get_session_lock(session_id):
            self._raise_if_quiesced()
            if startup_guard is not None:
                await startup_guard()
            existing = self._runtimes.get(session_id)
            existing = await self._drop_runtime_for_sandbox_mismatch(
                session_id,
                existing,
                sandbox_id,
                reason="ensure_runtime",
            )
            existing = await self._drop_runtime_for_unusable_client(
                session_id,
                existing,
                reason="ensure_runtime",
            )
            if existing is not None:
                return existing
            eff_sandbox = (
                ""
                if sandbox_id is _RUNTIME_SANDBOX_BINDING_UNSPECIFIED
                else str(sandbox_id or "").strip()
            )
            started_runtime = not bool(eff_sandbox)
            if eff_sandbox:
                attach_plan = workspace_plan or self.plan_runtime_attach(
                    agent_id=str(template.agent_id),
                    session_id=session_id,
                    session_kind=session_kind,
                    sandbox_id=eff_sandbox,
                    engine_session_key=engine_session_key,
                    existing_terminal_cwd=None,
                    engine_kind=str(getattr(template, "engine_kind", "") or ""),
                    runtime_identity=runtime_identity,
                )
                self._validate_runtime_attach_plan(session_id, eff_sandbox, attach_plan)
                from astrabox.core.service.orchestrator.engine.startup import (
                    attach_platform_runtime,
                )

                runtime = await attach_platform_runtime(
                    self,
                    get_engine_adapter(attach_plan.engine_kind),
                    session_id=session_id,
                    sandbox_id=eff_sandbox,
                    template=template,
                    workspace_plan=attach_plan,
                    user_id=user_id,
                    engine_session_key=engine_session_key,
                    permission_mode=permission_mode,
                    runtime_identity=runtime_identity,
                    attach_mode="full",
                )
            else:
                assignment = str(assignment_id or "").strip()
                if not assignment:
                    raise APIError(
                        code="SANDBOX_ASSIGNMENT_INVALID",
                        message="runtime creation requires a durable assignment_id",
                        status_code=500,
                    )
                if workspace_plan is None:
                    raise APIError(
                        code="WORKSPACE_PLAN_REQUIRED",
                        message="runtime start requires an explicit workspace plan",
                        status_code=500,
                    )
                self._validate_runtime_start_plan(session_id, workspace_plan)
                runtime = await self._start_runtime(
                    session_id=session_id,
                    template=template,
                    assignment_id=assignment,
                    user_id=user_id,
                    permission_mode=permission_mode,
                    progress_callback=progress_callback,
                    callback_url=callback_url,
                    workspace_plan=workspace_plan,
                )
                setattr(runtime, "permission_mode_verified", True)
            try:
                self._raise_if_quiesced()
                self._require_runtime_engine_manifest(runtime)
            except APIError as exc:
                if self._runtimes.get(session_id) is runtime:
                    self._runtimes.pop(session_id, None)
                await self._disconnect_runtime_client(
                    runtime,
                    session_id=session_id,
                    reason=self._quiesced_reason or "runtime publication failed",
                )
                if started_runtime and not self._quiesced_reason:
                    leaked_sandbox_id, cleanup_error = (
                        await self._rollback_runtime_start(session_id)
                    )
                    wrapped = self._runtime_start_error(
                        engine_kind=str(runtime.engine_kind or workspace_plan.engine_kind),
                        original=exc,
                        leaked_sandbox_id=leaked_sandbox_id,
                        cleanup_error=cleanup_error,
                    )
                    raise wrapped from exc
                raise
            if startup_guard is not None:
                try:
                    await startup_guard()
                except BaseException:
                    await self._disconnect_runtime_client(
                        runtime, session_id=session_id, reason="attachment owner changed",
                    )
                    raise
            runtime.user_id = str(user_id or "").strip() or None
            self._runtimes[session_id] = runtime
            return runtime

    async def ensure_runtime_lightweight(
        self,
        session_id: str,
        template,
        *,
        sandbox_id: str | None = None,
        engine_session_key: str | None = None,
        permission_mode: str | None = None,
        session_kind: str,
        workspace_plan: RuntimeWorkspacePlan | None = None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> "SessionRuntime":
        """Return an existing in-memory runtime or create a lightweight one.

        Unlike ``ensure_runtime``, this skips full workspace preparation. It is
        the turn-prepare attach path for
        agent_chat conversations and the resume attach path for existing
        sessions.  It still gates ``connect_only`` on sidecar revision, because
        a stale sidecar can expose a compatible protocol surface while missing
        newer initialization metadata.
        """
        self._raise_if_quiesced()
        existing = self.get_runtime(session_id, sandbox_id=sandbox_id)
        if existing is not None:
            return existing
        async with self._get_session_lock(session_id):
            self._raise_if_quiesced()
            existing = self._runtimes.get(session_id)
            existing = await self._drop_runtime_for_sandbox_mismatch(
                session_id,
                existing,
                sandbox_id,
                reason="ensure_runtime_lightweight",
            )
            existing = await self._drop_runtime_for_unusable_client(
                session_id,
                existing,
                reason="ensure_runtime_lightweight",
            )
            if existing is not None:
                return existing
            eff_sandbox = str(sandbox_id or "").strip()
            if not eff_sandbox:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="lightweight attach requires sandbox_id",
                    status_code=400,
                )
            attach_plan = workspace_plan or self.plan_runtime_attach(
                agent_id=str(template.agent_id),
                session_id=session_id,
                session_kind=session_kind,
                sandbox_id=eff_sandbox,
                engine_session_key=engine_session_key,
                existing_terminal_cwd=None,
                engine_kind=str(getattr(template, "engine_kind", "") or ""),
                runtime_identity=runtime_identity,
            )
            self._validate_runtime_attach_plan(session_id, eff_sandbox, attach_plan)
            from astrabox.core.service.orchestrator.engine.startup import (
                attach_platform_runtime,
            )

            runtime = await attach_platform_runtime(
                self,
                get_engine_adapter(attach_plan.engine_kind),
                session_id=session_id,
                sandbox_id=eff_sandbox,
                template=template,
                workspace_plan=attach_plan,
                user_id=None,
                engine_session_key=engine_session_key,
                permission_mode=permission_mode,
                runtime_identity=runtime_identity,
                attach_mode="lightweight",
            )
            try:
                self._raise_if_quiesced()
                self._require_runtime_engine_manifest(runtime)
            except APIError:
                if self._runtimes.get(session_id) is runtime:
                    self._runtimes.pop(session_id, None)
                await self._disconnect_runtime_client(
                    runtime,
                    session_id=session_id,
                    reason=self._quiesced_reason or "runtime publication failed",
                )
                raise
            runtime.user_id = None
            self._runtimes[session_id] = runtime
            return runtime

    async def evict_runtime(self, session_id: str) -> None:
        async with self._get_session_lock(session_id):
            runtime = self._runtimes.pop(session_id, None)
        if runtime is not None:
            await self._disconnect_evicted_runtime(
                runtime,
                session_id=session_id,
                reason="explicit_evict",
            )

    async def evict_runtime_if_current(
        self,
        session_id: str,
        expected_runtime: SessionRuntime,
    ) -> bool:
        """Evict one observed runtime without racing a concurrent replacement."""

        async with self._get_session_lock(session_id):
            if self._runtimes.get(session_id) is not expected_runtime:
                return False
            runtime = self._runtimes.pop(session_id)
        await self._disconnect_evicted_runtime(
            runtime,
            session_id=session_id,
            reason="sandbox_replacement",
        )
        return True

    async def dispose_runtime_session(
        self,
        session_id: str,
        *,
        sandbox_id: str | None = None,
        engine_kind: str | None = None,
        engine_turn_id: str | None = None,
    ) -> None:
        """Permanently dispose one conversation's resident engine process.

        This is deliberately separate from :meth:`evict_runtime`. Eviction
        only releases reconnectable client resources; this method is for
        lifecycle operations where the conversation is ending for good.
        When the in-memory client is gone, an engine may use the durable turn
        anchor to find and terminate its process in the shared sandbox.
        """
        async with self._get_session_lock(session_id):
            runtime = self._runtimes.pop(session_id, None)

        resolved_kind = str(engine_kind or "").strip().lower()
        if runtime is not None:
            resolved_kind = (
                str(getattr(runtime, "engine_kind", "") or "").strip().lower() or resolved_kind
            )
            with contextlib.suppress(BaseException):
                if runtime.current_task and not runtime.current_task.done():
                    runtime.current_task.cancel()
            disposed = await self._dispose_runtime_turn_client(runtime)
            if disposed:
                logger.info(
                    "disposed resident engine process session=%s engine=%s source=live-client",
                    session_id,
                    resolved_kind or "<unknown>",
                )
                return

        normalized_sandbox_id = str(sandbox_id or "").strip()
        normalized_engine_turn_id = str(engine_turn_id or "").strip()
        if not (resolved_kind and normalized_sandbox_id and normalized_engine_turn_id):
            logger.info(
                "conversation runtime disposed without a durable resident process "
                "anchor session=%s engine=%s sandbox=%s",
                session_id,
                resolved_kind or "<unknown>",
                normalized_sandbox_id or "<unknown>",
            )
            return

        disposal = get_engine_adapter(resolved_kind).process_disposal()
        if not isinstance(disposal, EngineProcessDisposalCapability):
            logger.info(
                "engine has no durable resident process disposal capability session=%s engine=%s",
                session_id,
                resolved_kind,
            )
            return
        await disposal.dispose_process(
            sandbox_id=normalized_sandbox_id,
            engine_turn_id=normalized_engine_turn_id,
        )
        logger.info(
            "disposed resident engine process session=%s engine=%s source=durable-anchor",
            session_id,
            resolved_kind,
        )

    async def dispose_terminal_session(
        self,
        session_id: str,
        *,
        sandbox_id: str | None,
        pty_session_id: str | None,
    ) -> None:
        """Terminate the persistent shell owned by one conversation."""
        normalized_sandbox_id = str(sandbox_id or "").strip()
        normalized_pty_session_id = str(pty_session_id or "").strip()
        if not normalized_sandbox_id or not normalized_pty_session_id:
            return
        sandbox = await self.connect_sandbox_only(normalized_sandbox_id)
        try:
            endpoint = await resolve_execd_endpoint(get_underlying_sandbox(sandbox))
            terminal = PtyTerminal(
                endpoint.origin,
                headers=endpoint.headers,
            )
            await terminal.close_session(normalized_pty_session_id)
        finally:
            close = getattr(sandbox, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()
        logger.info(
            "disposed conversation terminal PTY session=%s sandbox=%s pty=%s",
            session_id,
            normalized_sandbox_id,
            normalized_pty_session_id,
        )

    async def terminate_runtime(
        self, session_id: str, fallback_sandbox_id: str | None = None
    ) -> SandboxDestruction:
        """Tear this session's runtime down and destroy the box it was bound to.

        The verdict is about one sandbox — the one the caller asked about — and
        it is what licenses the caller to release that sandbox's last name
        (:func:`~astrabox.seams.sandbox_disposal.may_sever_last_name`). It is
        not a bool, because the three answers this can give are "gone",
        "possibly still running" and "nothing to destroy", and only the first
        of them may clear a pointer.

        Which box. The runtime this process holds is evidence: it was built
        against a real box and carries its id. ``fallback_sandbox_id`` is a
        request from a caller that read it off a row which may since have
        moved. When they disagree, believing only the caller and discarding
        the runtime unkilled would destroy just the id it was handed, leaving
        a stale pointer with a live box torn down at one end and left running
        at the other. So both are destroyed — the runtime's, because this
        process is the last thing holding it, and the requested one, because
        that is what the caller will act on — and the verdict returned is the
        requested box's, so no pointer is released on the strength of a
        different box's death.
        """
        # Runtime creation and registration happen while holding this same
        # lock. Never bypass it on a timer: doing so lets a slow create finish
        # after deletion has already returned, registering a live sandbox with
        # no remaining Session owner. The create path is itself bounded by the
        # configured sandbox readiness deadline; cancellation while waiting
        # leaves both maps untouched so the lifecycle command can be retried.
        async with self._get_session_lock(session_id):
            runtime = self._runtimes.pop(session_id, None)

        pending_ids = self._pending_startup_sandbox_ids(session_id)
        requested_id = str(fallback_sandbox_id or "").strip() or None
        target_id = requested_id or (pending_ids[0] if len(pending_ids) == 1 else None)

        if runtime is None:
            if target_id:
                persisted_placement = await self._release_persisted_placement(
                    session_id=session_id,
                    sandbox_id=target_id,
                )
                if persisted_placement is not None:
                    await self._release_closed_admission(session_id, target_id)
                    if not await self.agent_box_has_other_occupants(
                        target_id, excluding=session_id
                    ):
                        return await self._destroy_and_forget_pending(session_id, target_id)
                    return persisted_placement
                return await self._destroy_and_forget_pending(session_id, target_id)
            if pending_ids:
                # More than one box was created for this session and none was
                # named: destroying "the pending one" would be a guess between
                # them. Destroy all of them — each is unambiguously this session's
                # and unambiguously not a live runtime — and report the last
                # unconfirmed one so nothing upstream reads success.
                return await self._destroy_all_pending(session_id, pending_ids)
            return SandboxDestruction.nothing_named(
                detail=(
                    f"session {session_id} has no runtime in this process, no "
                    "sandbox id was given, and none was recorded for it"
                )
            )
        mismatch = self._runtime_sandbox_mismatch(
            runtime,
            target_id or _RUNTIME_SANDBOX_BINDING_UNSPECIFIED,
        )
        if mismatch is not None:
            runtime_sandbox_id, expected_id = mismatch
            logger.warning(
                "discard stale runtime during terminate session=%s runtime_sandbox=%s expected_sandbox=%s",
                session_id,
                runtime_sandbox_id or "<missing>",
                expected_id,
            )
            await self._disconnect_evicted_runtime(
                runtime,
                session_id=session_id,
                reason="terminate_runtime_sandbox_mismatch",
            )
            if runtime_sandbox_id:
                # The runtime's own box is this process's to destroy, and no
                # other owner is left to notice it. Returning from this branch
                # without killing it would leak the box, so the destroy below
                # is unconditional.
                stale = await self._destroy_and_forget_pending(session_id, runtime_sandbox_id)
                if not stale.confirmed:
                    logger.error(
                        "terminate session=%s discarded a stale runtime whose "
                        "sandbox %s was not confirmed destroyed: %s",
                        session_id,
                        runtime_sandbox_id,
                        stale.detail,
                    )
            return await self._destroy_and_forget_pending(session_id, expected_id)

        with contextlib.suppress(BaseException):
            if runtime.current_task and not runtime.current_task.done():
                runtime.current_task.cancel()

        T = self._TERMINATE_OP_TIMEOUT_SECONDS
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(
                self._close_runtime_turn_client(runtime),
                timeout=T,
            )
        if runtime.agent is not None:
            try:
                await self._await_on_owner_loop(
                    runtime,
                    asyncio.wait_for(runtime.agent.stop(), timeout=T),
                    action="terminate.agent.stop",
                )
            except BaseException as exc:
                logger.warning("agent stop failed session=%s err=%s", session_id, exc)

        underlying = get_underlying_sandbox(runtime.sandbox)
        if underlying is not None:
            # close() is optional on SandboxHandle: getattr-guard so its absence
            # is a normal shape, not a fake AttributeError; suppress(Exception)
            # — never BaseException, which would swallow CancelledError
            # mid-shutdown. The handle's own kill() is deliberately not used as
            # the destruction: it reports only that the SDK call returned, and a
            # destruction that nothing observed twice may not clear a pointer.
            close = getattr(underlying, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(close(), timeout=T)

        destroy_id = str(runtime.sandbox_id or target_id or "").strip()
        if runtime.isolated_session_id:
            # Release this conversation's isolated session first. Destroy the
            # shared sandbox only when no occupants remain; retaining empty
            # sandboxes would consume capacity until their leases expire.
            released = await self._release_runtime_placement(runtime, session_id=session_id)
            if released and destroy_id:
                await self._release_closed_admission(session_id, destroy_id)
            detail = (
                f"session {session_id} ran in isolated session "
                f"{runtime.isolated_session_id} of box {destroy_id or '<unknown>'}; "
            )
            if released and destroy_id and not await self.agent_box_has_other_occupants(
                destroy_id, excluding=session_id
            ):
                return await self._destroy_and_forget_pending(session_id, destroy_id)
            if released:
                return SandboxDestruction.retained(
                    destroy_id,
                    detail=(
                        detail + "the session was closed and the agent-owned box remains "
                        "durably named"
                    ),
                )
            return SandboxDestruction.refused(
                destroy_id,
                detail=(
                    detail + "the isolated session could not be confirmed closed, so "
                    "the box address must be retained for reconciliation"
                ),
            )
        if not destroy_id:
            return SandboxDestruction.nothing_named(
                detail=(
                    f"session {session_id} had a runtime bound to no sandbox id, and none was given"
                )
            )
        return await self._destroy_and_forget_pending(session_id, destroy_id)

    async def _release_closed_admission(self, session_id: str, sandbox_id: str) -> None:
        """Withdraw a closed placement even after its Agent prefers another box."""

        from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
            release_box_admission,
        )
        from astrabox.persistence.repository.agent_repository import AgentRepository

        session = await self._sessions_repo.get_session_including_deleted(session_id)
        agent_id = str((session or {}).get("agent_id") or "").strip()
        if agent_id:
            await release_box_admission(
                agent_repo=AgentRepository(),
                agent_id=agent_id,
                sandbox_id=sandbox_id,
                session_id=session_id,
            )

    async def _release_persisted_placement(
        self,
        *,
        session_id: str,
        sandbox_id: str,
    ) -> SandboxDestruction | None:
        """Close a shared conversation's isolated session after host restart."""
        session = await SessionRepository().get_session(session_id)
        if not isinstance(session, dict):
            return None
        if str(session.get("sandbox_id") or "").strip() != str(sandbox_id):
            return None
        identity = session.get("runtime_identity")
        if not isinstance(identity, dict):
            return None
        isolated_session_id = str(identity.get("isolated_session_id") or "").strip()
        if not isolated_session_id:
            return None
        terminal_isolated_session_id = str(
            identity.get("terminal_isolated_session_id") or ""
        ).strip()
        backend = str(session.get("sandbox_backend") or "").strip().lower()
        if not backend:
            backend = await self._resolve_sandbox_backend(sandbox_id)
        provider = sandbox_for_name(backend)
        failure: Exception | None = None
        for child_id in (terminal_isolated_session_id, isolated_session_id):
            if not child_id:
                continue
            try:
                await provider.close_isolated_session(sandbox_id, child_id)
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
        logger.info(
            "terminate closed persisted isolated sessions after restart "
            "session=%s agent_iso=%s terminal_iso=%s box=%s",
            session_id,
            isolated_session_id,
            terminal_isolated_session_id or "<none>",
            sandbox_id,
        )
        return SandboxDestruction.retained(
            sandbox_id,
            detail=(
                f"session {session_id} used isolated session "
                f"{isolated_session_id} of shared box {sandbox_id}; the session "
                "was closed and the agent-owned box was left running"
            ),
        )

    async def _destroy_and_forget_pending(
        self, session_id: str, sandbox_id: str
    ) -> SandboxDestruction:
        """Destroy one box; drop its pending name only once that is CONFIRMED.

        The pairing rule applied to this process's own memory: the pending set
        is a name, and the in-memory name is the last one a box has before its
        id reaches a row. Forgetting it on an unconfirmed destroy is the same
        defect as clearing a persisted pointer on one.
        """
        destruction = await self.destroy_sandbox_by_id(sandbox_id)
        if destruction.confirmed:
            self._forget_pending_sandbox(session_id, sandbox_id)
        return destruction

    async def _destroy_all_pending(
        self, session_id: str, pending_ids: list[str]
    ) -> SandboxDestruction:
        last_unconfirmed: SandboxDestruction | None = None
        for pending in pending_ids:
            destruction = await self._destroy_and_forget_pending(session_id, pending)
            if not destruction.confirmed:
                last_unconfirmed = destruction
                logger.error(
                    "terminate session=%s could not confirm the destruction of "
                    "sandbox %s recorded for it: %s",
                    session_id,
                    pending,
                    destruction.detail,
                )
        if last_unconfirmed is not None:
            return last_unconfirmed
        return SandboxDestruction.confirmed_gone(
            pending_ids[-1],
            detail=(
                f"every sandbox recorded for session {session_id} "
                f"({', '.join(pending_ids)}) was confirmed destroyed"
            ),
        )

    def _forget_pending_sandbox(self, session_id: str, sandbox_id: str) -> None:
        pending = self._pending_startup_allocations.get(session_id)
        if not pending:
            return
        target = str(sandbox_id or "").strip()
        pending[:] = [item for item in pending if item.sandbox_id != target]
        if not pending:
            self._pending_startup_allocations.pop(session_id, None)

    def _forget_pending_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
    ) -> None:
        pending = self._pending_startup_allocations.get(session_id)
        if not pending:
            return
        pending[:] = [item for item in pending if item != allocation]
        if not pending:
            self._pending_startup_allocations.pop(session_id, None)

    def _remember_pending_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
    ) -> None:
        pending = self._pending_startup_allocations.setdefault(session_id, [])
        if allocation not in pending:
            pending.append(allocation)

    def _pending_startup_sandbox_ids(self, session_id: str) -> list[str]:
        return sorted(
            {item.sandbox_id for item in self._pending_startup_allocations.get(session_id, ())}
        )

    # ── Interrupt ──────────────────────────────────────────────────

    async def _interrupt_sandbox_execution(
        self,
        sandbox_obj: Any,
        *,
        execution_id: str,
    ) -> None:
        command_runner = getattr(sandbox_obj, "commands", None) if sandbox_obj is not None else None
        interrupt_fn = (
            getattr(command_runner, "interrupt", None) if command_runner is not None else None
        )
        if not callable(interrupt_fn):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="interrupt failed: terminal execution handle unavailable",
                status_code=502,
            )
        try:
            await interrupt_fn(execution_id)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"interrupt failed: {exc}",
                status_code=502,
            ) from exc

    async def _interrupt_runtime_execution(
        self,
        runtime: SessionRuntime,
        *,
        execution_id: str,
    ) -> None:
        if is_isolated_terminal_execution_id(execution_id):
            await self._interrupt_isolated_terminal_execution(execution_id)
            return
        sandbox_obj = get_underlying_sandbox(runtime.sandbox) or runtime.sandbox
        await self._interrupt_sandbox_execution(
            sandbox_obj,
            execution_id=execution_id,
        )

    async def interrupt_terminal_execution(
        self,
        session_id: str,
        *,
        execution_id: str,
        sandbox_id: str | None = None,
    ) -> None:
        eff_execution_id = str(execution_id or "").strip()
        if not eff_execution_id:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="interrupt failed: terminal execution handle unavailable",
                status_code=502,
            )
        if is_isolated_terminal_execution_id(eff_execution_id):
            await self._interrupt_isolated_terminal_execution(eff_execution_id)
            runtime = self._runtimes.get(session_id)
            if (
                runtime is not None
                and str(getattr(runtime, "current_execution_id", "") or "").strip()
                == eff_execution_id
            ):
                runtime.current_execution_id = None
            return

        runtime = self._runtimes.get(session_id)
        runtime = await self._drop_runtime_for_sandbox_mismatch(
            session_id,
            runtime,
            sandbox_id or _RUNTIME_SANDBOX_BINDING_UNSPECIFIED,
            reason="interrupt_terminal_execution",
        )
        if runtime is not None and runtime.sandbox is not None:
            await self._interrupt_runtime_execution(
                runtime,
                execution_id=eff_execution_id,
            )
            if str(getattr(runtime, "current_execution_id", "") or "").strip() == eff_execution_id:
                runtime.current_execution_id = None
            return

        eff_sandbox_id = (
            str(sandbox_id or "").strip()
            or str(getattr(runtime, "sandbox_id", "") or "").strip()
            or None
        )
        if not eff_sandbox_id:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="interrupt failed: terminal sandbox unavailable",
                status_code=502,
            )

        sandbox_obj = await self.connect_sandbox_only(eff_sandbox_id)
        try:
            await self._interrupt_sandbox_execution(
                sandbox_obj,
                execution_id=eff_execution_id,
            )
        finally:
            disconnect_fn = getattr(sandbox_obj, "disconnect", None)
            close_fn = getattr(sandbox_obj, "close", None)
            with contextlib.suppress(Exception):
                if callable(disconnect_fn):
                    maybe_awaitable = disconnect_fn()
                    if asyncio.iscoroutine(maybe_awaitable):
                        await maybe_awaitable
            with contextlib.suppress(Exception):
                if callable(close_fn):
                    maybe_awaitable = close_fn()
                    if asyncio.iscoroutine(maybe_awaitable):
                        await maybe_awaitable

    def _is_terminal_sandbox_lifecycle_probe(
        self,
        probe: SandboxLifecycleProbeResult,
    ) -> bool:
        probe_status = str(getattr(probe, "probe_status", "") or "").strip()
        if probe_status == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND:
            return True
        # A known terminal lifecycle state is authoritative whether the provider
        # tagged the probe OK or PROBE_FAILED. A backend may report a
        # stopped/exited/dead container as PROBE_FAILED(sandbox_state="exited"|
        # "dead"|"stopped") after a successful inspect — a confirmed death that must
        # converge. A transient PROBE_FAILED (dockerd unreachable, backend
        # unresolved, or a timed-out probe) carries an empty sandbox_state and stays
        # non-terminal, so it never converges a live binding.
        #
        # A parked box is deliberately absent from that set: this predicate decides
        # whether to stop naming a sandbox, and a paused one is the case where the
        # name is the only way back to the files. See _PARKED_SANDBOX_STATES.
        return (
            str(getattr(probe, "sandbox_state", None) or "").strip().lower()
            in _SANDBOX_RESOURCE_GONE_STATES
        )

    def clear_interrupting(self, session_id: str) -> None:
        rt = self._runtimes.get(session_id)
        if rt is not None:
            rt.interrupting = False

    # ── Agent startup orchestration ────────────────────────────────

    def _sandbox_ready_timeout_seconds(self) -> int:
        return max(1, int(self._settings.sandbox_ready_timeout_seconds))

    async def _record_startup_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
        *,
        replaces: SandboxAllocation | None = None,
    ) -> None:
        """Name a provisioned resource durably before startup can await again.

        Memory is written first so an ordinary exception can still clean up if
        the database write itself fails. The failed durable write is then
        raised: continuing would turn a process restart into an unaddressable
        resource. A whole sandbox and isolated sessions deliberately share this
        path, while their different release authority remains in ``scope``.
        """

        target_session = str(session_id or "").strip()
        if not target_session:
            raise ValueError("recording a startup allocation requires session_id")
        ownership = current_recovery(target_session)
        if ownership is not None:
            if allocation not in ownership.allocations:
                ownership.allocations.append(allocation)
        self._remember_pending_allocation(target_session, allocation)
        self._sandbox_backend_cache[allocation.sandbox_id] = (
            allocation.sandbox_backend
        )
        try:
            if ownership is not None:
                await ownership.require_current()
            if replaces is not None and replaces.sandbox_id != allocation.sandbox_id:
                # A competing shared-box claim can win after this startup borrowed
                # a candidate. Keep both names until the unused candidate is gone;
                # the new isolated sessions are already in the local rollback set.
                provider = sandbox_for_name(replaces.sandbox_backend)
                destruction = await provider.confirm_destroyed(replaces.sandbox_id)
                if not destruction.confirmed:
                    raise APIError(
                        code="SANDBOX_CLEANUP_UNCONFIRMED",
                        message=(
                            f"unused startup candidate {replaces.sandbox_id!r} could "
                            f"not be destroyed: {destruction.detail}"
                        ),
                        status_code=502,
                        data={"leaked_sandbox_id": replaces.sandbox_id},
                    )
            if replaces is None:
                await self._sessions_repo.record_startup_allocation(
                    target_session,
                    allocation.as_record(),
                    **({"owner_expected": ownership.expected} if ownership is not None else {}),
                )
            else:
                await self._sessions_repo.record_startup_allocation(
                    target_session,
                    allocation.as_record(),
                    replaces=replaces.as_record(),
                    **({"owner_expected": ownership.expected} if ownership is not None else {}),
                )
            if replaces is not None and replaces != allocation:
                self._forget_pending_allocation(target_session, replaces)
        except BaseException as exc:
            if ownership is not None:
                record = {
                    "allocation": allocation.as_record(),
                    "sandbox_generation": ownership.sandbox_generation,
                    "assignment_id": ownership.assignment_id,
                }
                try:
                    await self._sessions_repo.retain_startup_allocation(target_session, record)
                except BaseException as retention_error:
                    logger.error(
                        "startup allocation retention failed session=%s receipt=%s",
                        target_session,
                        record,
                        exc_info=True,
                    )
                    raise APIError(
                        code="SANDBOX_CLEANUP_UNCONFIRMED",
                        message=f"startup allocation could not be published or retained: {retention_error}",
                        status_code=502,
                        data={"retained_allocation": record, "retention_confirmed": False},
                    ) from exc
            raise

    async def _load_startup_allocation(
        self,
        session_id: str,
    ) -> SandboxAllocation | None:
        session = await self._sessions_repo.get_session_including_deleted(session_id)
        raw = (session or {}).get("startup_allocation")
        if raw is None:
            return None
        try:
            allocation = SandboxAllocation.from_record(raw)
        except ValueError as exc:
            raise APIError(
                code="STARTUP_ALLOCATION_INVALID",
                message=(
                    f"session {session_id!r} has an invalid startup allocation: {exc}"
                ),
                status_code=500,
            ) from exc
        self._sandbox_backend_cache[allocation.sandbox_id] = (
            allocation.sandbox_backend
        )
        return allocation

    async def _complete_startup_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
    ) -> None:
        """Drop a startup name only after release or durable owner publication."""

        cleared = await self._sessions_repo.clear_startup_allocation(
            session_id,
            allocation=allocation.as_record(),
        )
        if not cleared:
            raise RuntimeError(
                "startup allocation moved before completion "
                f"session={session_id!r} sandbox={allocation.sandbox_id!r}"
            )
        self._forget_pending_allocation(session_id, allocation)

    async def _finish_released_startup_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
        *,
        clear_durable_record: bool,
    ) -> tuple[bool, str]:
        """Forget one released resource without severing another attempt's name."""

        self._forget_pending_allocation(session_id, allocation)
        if not clear_durable_record:
            return True, ""
        try:
            await self._complete_startup_allocation(
                session_id,
                allocation,
            )
        except BaseException as exc:
            return (
                False,
                "resource released but its startup allocation record could not "
                f"be cleared: {type(exc).__name__}: {exc}",
            )
        return True, ""

    async def _allocation_box_is_gone(
        self,
        allocation: SandboxAllocation,
    ) -> bool:
        provider = sandbox_for_name(allocation.sandbox_backend)
        try:
            probe = await provider.probe(allocation.sandbox_id)
        except NotImplementedError:
            return False
        except Exception:
            return False
        return str(getattr(probe, "probe_status", "") or "").strip() == (
            SANDBOX_LIFECYCLE_PROBE_NOT_FOUND
        )

    async def _release_startup_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
        *,
        clear_durable_record: bool,
    ) -> StartupAllocationCleanup:
        """Release exactly the resource scope named by one allocation."""

        ownership = current_recovery(session_id)
        if allocation.scope == "sandbox":
            sessions, workspaces, agent = await asyncio.gather(
                self._sessions_repo.list_sessions_by_sandbox_id(allocation.sandbox_id),
                AssistantWorkspaceRepository().list_workspaces_by_sandbox_id(allocation.sandbox_id),
                AgentRepository().find_agent_by_sandbox_id(allocation.sandbox_id),
            )
            if sessions or workspaces or agent:
                if ownership is not None:
                    record = {
                        "allocation": allocation.as_record(),
                        "sandbox_generation": ownership.sandbox_generation,
                        "assignment_id": ownership.assignment_id,
                    }
                    await self._sessions_repo.retain_startup_allocation(session_id, record)
                # Background reconciliation has no task context. Its exact
                # durable startup_allocation remains the name for this scope.
                destruction = SandboxDestruction.refused(
                    allocation.sandbox_id,
                    detail="startup allocation is retained because a durable runtime owner binds the box",
                )
                return StartupAllocationCleanup(
                    allocation=allocation,
                    released=False,
                    record_cleared=False,
                    destruction=destruction,
                    detail=destruction.detail,
                )

        destruction: SandboxDestruction | None = None
        if await self._allocation_box_is_gone(allocation):
            destruction = SandboxDestruction.confirmed_gone(
                allocation.sandbox_id,
                detail="the provider no longer reports the allocation's sandbox",
            )
        else:
            runtime = self._runtimes.get(session_id)
            if (
                current_recovery(session_id) is None
                and runtime is not None
                and str(runtime.sandbox_id or "").strip() == allocation.sandbox_id
            ):
                destruction = await self.terminate_runtime(
                    session_id,
                    fallback_sandbox_id=allocation.sandbox_id,
                )
            elif allocation.scope == "isolated_sessions":
                provider = sandbox_for_name(allocation.sandbox_backend)
                try:
                    for isolated_session_id in allocation.isolated_session_ids:
                        await provider.close_isolated_session(
                            allocation.sandbox_id,
                            isolated_session_id,
                        )
                except Exception as exc:
                    return StartupAllocationCleanup(
                        allocation=allocation,
                        released=False,
                        record_cleared=False,
                        detail=(
                            "isolated-session release failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                destruction = SandboxDestruction.retained(
                    allocation.sandbox_id,
                    detail=(
                        "the startup's isolated sessions were closed and the "
                        "longer-lived sandbox was retained"
                    ),
                )
            else:
                provider = sandbox_for_name(allocation.sandbox_backend)
                try:
                    claim = await provider.claim_of(
                        allocation.sandbox_id,
                        expected_session_id=session_id,
                    )
                except Exception as exc:
                    destruction = SandboxDestruction.refused(
                        allocation.sandbox_id,
                        detail=f"the startup allocation claim failed: {exc}",
                    )
                else:
                    if not claim.may_destroy:
                        destruction = SandboxDestruction.refused(
                            allocation.sandbox_id,
                            detail=claim.detail,
                        )
                        # A pooled candidate moves to the Agent before its
                        # isolated placement is published. The Session may
                        # relinquish that reservation, never destroy the box
                        # now owned by the Agent and possibly its siblings.
                        from astrabox.core.service.orchestrator.agent.runtime_generation import (
                            agent_runtime_owner_id,
                        )

                        session = await self._sessions_repo.get_session_including_deleted(
                            session_id
                        )
                        agent_id = str((session or {}).get("agent_id") or "").strip()
                        if agent_id:
                            agent_claim = await provider.claim_of(
                                allocation.sandbox_id,
                                expected_session_id=agent_runtime_owner_id(agent_id),
                            )
                            if agent_claim.may_destroy:
                                destruction = SandboxDestruction.retained(
                                    allocation.sandbox_id,
                                    detail="startup reservation handed to its Agent owner",
                                )
                    else:
                        destruction = await self._destroy_and_forget_pending(
                            session_id,
                            allocation.sandbox_id,
                        )

        released = destruction.confirmed or destruction.outcome == "RETAINED"
        if not released:
            return StartupAllocationCleanup(
                allocation=allocation,
                released=False,
                record_cleared=False,
                destruction=destruction,
                detail=destruction.detail,
            )
        record_cleared, record_detail = await self._finish_released_startup_allocation(
            session_id,
            allocation,
            clear_durable_record=clear_durable_record,
        )
        return StartupAllocationCleanup(
            allocation=allocation,
            released=True,
            record_cleared=record_cleared,
            destruction=destruction,
            detail=record_detail or destruction.detail,
        )

    async def _cleanup_pending_startup_allocations(
        self,
        session_id: str,
    ) -> StartupAllocationCleanup:
        """Roll back only allocations created by this manager process."""

        ownership = current_recovery(session_id)
        pending = (tuple(ownership.allocations) if ownership is not None
                   else tuple(self._pending_startup_allocations.get(session_id, ())))
        if not pending:
            destruction = SandboxDestruction.nothing_named(
                detail=(
                    f"startup cleanup for session={session_id} had no "
                    "process-owned allocation to release"
                )
            )
            return StartupAllocationCleanup(
                allocation=None,
                released=True,
                destruction=destruction,
                detail=destruction.detail,
            )

        results: list[StartupAllocationCleanup] = []
        for allocation in pending:
            results.append(
                await self._release_startup_allocation(
                    session_id,
                    allocation,
                    clear_durable_record=True,
                )
            )
        failures = [
            result
            for result in results
            if not result.released or not result.record_cleared
        ]
        if failures:
            first = failures[0]
            return StartupAllocationCleanup(
                allocation=first.allocation,
                released=all(result.released for result in results),
                record_cleared=all(result.record_cleared for result in results),
                destruction=first.destruction,
                detail="; ".join(result.detail for result in failures if result.detail),
            )
        last = results[-1]
        return StartupAllocationCleanup(
            allocation=last.allocation if len(results) == 1 else None,
            released=True,
            record_cleared=True,
            destruction=last.destruction,
            detail="; ".join(result.detail for result in results if result.detail),
        )

    async def cleanup_startup_allocation(
        self,
        session_id: str,
        *,
        fallback_sandbox_id: str | None = None,
    ) -> StartupAllocationCleanup:
        """Release a startup resource from its durable scope after a restart.

        Whole-box records are destroyed only after the provider's ownership
        metadata names this Session. Shared records close only their exact
        isolated sessions. The latter never falls through to box destruction:
        failure keeps the allocation record for the next reconciliation tick.
        """

        if current_recovery(session_id) is not None:
            # A recovery may resume after another host started a new generation.
            # Its context names only the resources this exact attempt created.
            return await self._cleanup_pending_startup_allocations(session_id)
        allocation = await self._load_startup_allocation(session_id)
        fallback = str(fallback_sandbox_id or "").strip() or None
        if allocation is None:
            pending = tuple(self._pending_startup_allocations.get(session_id, ()))
            if pending:
                if fallback and any(item.sandbox_id != fallback for item in pending):
                    return StartupAllocationCleanup(
                        allocation=pending[0],
                        released=False,
                        record_cleared=False,
                        destruction=SandboxDestruction.refused(
                            pending[0].sandbox_id,
                            detail=(
                                "startup cleanup received a sandbox different from "
                                f"a process-owned allocation: fallback={fallback!r}"
                            ),
                        ),
                        detail="startup allocation sandbox mismatch",
                    )
                return await self._cleanup_pending_startup_allocations(session_id)
            if self._runtimes.get(session_id) is not None:
                destruction = await self.terminate_runtime(
                    session_id,
                    fallback_sandbox_id=fallback,
                )
            else:
                if fallback:
                    destruction = await self._destroy_and_forget_pending(
                        session_id,
                        fallback,
                    )
                else:
                    destruction = SandboxDestruction.nothing_named(
                        detail=(
                            f"startup cleanup for session={session_id} had no "
                            "runtime or allocation to release"
                        )
                    )
            released = destruction.confirmed or destruction.outcome == "RETAINED"
            return StartupAllocationCleanup(
                allocation=None,
                released=released,
                record_cleared=True,
                destruction=destruction,
                detail=destruction.detail,
            )

        if fallback and fallback != allocation.sandbox_id:
            return StartupAllocationCleanup(
                allocation=allocation,
                released=False,
                record_cleared=False,
                destruction=SandboxDestruction.refused(
                    allocation.sandbox_id,
                    detail=(
                        "startup cleanup received a sandbox different from its "
                        f"durable allocation: fallback={fallback!r}"
                    ),
                ),
                detail="startup allocation sandbox mismatch",
            )

        return await self._release_startup_allocation(
            session_id,
            allocation,
            clear_durable_record=True,
        )

    async def reap_ownerless_sandboxes(
        self,
        *,
        backend: str = "open_sandbox",
        grace_seconds: float = 600.0,
        limit: int = 100,
    ) -> dict[str, int]:
        """Give back every managed box that no row names any more.

        The population the row-driven sweep cannot see: a lend abandoned by
        its client (the server finished the Pod after the caller's deadline)
        exists only in the control plane — no session ever adopted it, no
        agent row points at it. Measured after one saturated lane, twelve of
        fourteen live boxes were exactly this, each holding ~2 GiB on a
        four-hour lease. The grace keeps a box that was created moments ago
        for a session still writing its rows.
        """

        from datetime import datetime, timezone

        from astrabox.persistence.repository.agent_repository import (
            AgentRepository,
        )

        summary = {
            "ownerless_scanned": 0,
            "ownerless_reaped": 0,
            "ownerless_kept": 0,
            "ownerless_reap_failures": 0,
        }
        try:
            provider = sandbox_for_name(backend)
            page = await provider.list_sandboxes(page=1, page_size=limit)
        except Exception:
            logger.exception("ownerless reap could not list the inventory")
            summary["ownerless_reap_failures"] += 1
            return summary
        agent_repo = AgentRepository()
        now = datetime.now(timezone.utc)
        for descriptor in getattr(page, "items", ()):
            sandbox_id = str(getattr(descriptor, "sandbox_id", "") or "").strip()
            if not sandbox_id:
                continue
            summary["ownerless_scanned"] += 1
            try:
                if (
                    descriptor.metadata.get(SANDBOX_MANAGED_BY_METADATA_KEY)
                    != SANDBOX_MANAGED_BY_METADATA_VALUE
                    or provider.owns_unclaimed_sandbox(descriptor)
                ):
                    summary["ownerless_kept"] += 1
                    continue
                created_at = getattr(descriptor, "created_at", None)
                if created_at is not None:
                    age = (now - created_at).total_seconds()
                    if age < grace_seconds:
                        summary["ownerless_kept"] += 1
                        continue
                owner = await self._sessions_repo.find_session_by_sandbox_id(sandbox_id)
                if owner is not None:
                    summary["ownerless_kept"] += 1
                    continue
                agents = await agent_repo.list_agents_by_sandbox_id(sandbox_id)
                if agents:
                    summary["ownerless_kept"] += 1
                    continue
                if await self.agent_box_has_other_occupants(
                    sandbox_id, excluding="", provider=provider
                ):
                    summary["ownerless_kept"] += 1
                    continue
                destruction = await provider.confirm_destroyed(sandbox_id)
                if destruction.confirmed:
                    summary["ownerless_reaped"] += 1
                    logger.info(
                        "ownerless reap: gave back box=%s (no session or "
                        "agent row names it)",
                        sandbox_id,
                    )
                else:
                    summary["ownerless_reap_failures"] += 1
                    logger.warning(
                        "ownerless reap could not confirm destruction "
                        "box=%s: %s",
                        sandbox_id,
                        destruction.detail,
                    )
            except Exception:
                logger.exception("ownerless reap failed box=%s", sandbox_id)
                summary["ownerless_reap_failures"] += 1
        return summary

    async def keep_prewarmed_agents_ready(
        self, *, limit: int = 100
    ) -> dict[str, int]:
        """Maintain prewarmed capacity without waiting for Session activity.

        Renew shared sandbox leases, schedule replacement of expiring prepared
        slots, and schedule rebuilds for Agents without a slot. Missing-slot
        rebuilds use a retry window so a persistent failure does not trigger a
        build on every sweep. Return counts of attempted work and failures.
        """
        from astrabox.core.service.orchestrator.agent.prepared_slots import (
            PREPARED_SLOT_FIELD,
            PREPARED_SLOT_REBUILD_RETRY_SECONDS,
            prepared_slot_is_due_for_renewal,
        )
        from astrabox.persistence.repository.agent_repository import (
            AgentRepository,
        )

        summary = {
            "prewarm_agents_scanned": 0,
            "agent_box_leases_renewed": 0,
            "prepared_slots_renewed": 0,
            "prepared_slots_rebuild_scheduled": 0,
            "prewarm_sweep_failures": 0,
        }
        repo = AgentRepository()
        try:
            rows = await repo.list_prewarm_enabled_agents(limit=limit)
        except Exception:
            logger.exception("prewarm sweep could not list prewarm-enabled Agents")
            summary["prewarm_sweep_failures"] += 1
            return summary
        lease = int(getattr(self._settings, "sandbox_lease_seconds", 14400))
        threshold = int(
            getattr(self._settings, "sandbox_lease_renew_threshold_seconds", 3600)
        )
        now = time.monotonic()
        for row in rows:
            agent_id = str((row or {}).get("agent_id") or "").strip()
            if not agent_id:
                continue
            summary["prewarm_agents_scanned"] += 1
            sandbox_id = str((row or {}).get("sandbox_id") or "").strip()
            if sandbox_id:
                # renew sets an absolute now+lease, so renewing once per
                # (lease - threshold) keeps at least `threshold` in hand
                # between sweeps without a call every tick.
                last = self._prewarm_lease_renewed_at.get(sandbox_id)
                if last is None or now - last >= max(1, lease - threshold):
                    try:
                        await self.renew_sandbox_by_id(sandbox_id, lease)
                    except Exception as exc:
                        logger.warning(
                            "prewarm sweep could not renew the box lease: agent=%s sandbox=%s: %s",
                            agent_id,
                            sandbox_id,
                            exc,
                        )
                        summary["prewarm_sweep_failures"] += 1
                    else:
                        self._prewarm_lease_renewed_at[sandbox_id] = now
                        summary["agent_box_leases_renewed"] += 1
            manifest = (row or {}).get(PREPARED_SLOT_FIELD)
            if isinstance(manifest, dict):
                reason = prepared_slot_is_due_for_renewal(manifest)
                if reason is None:
                    continue
                counter = "prepared_slots_renewed"
            else:
                last_rebuild = self._prewarm_rebuild_scheduled_at.get(agent_id)
                if (
                    last_rebuild is not None
                    and now - last_rebuild < PREPARED_SLOT_REBUILD_RETRY_SECONDS
                ):
                    continue
                reason = "no prepared slot"
                counter = "prepared_slots_rebuild_scheduled"
            try:
                started = self.schedule_agent_runtime_reconciliation(agent_id)
            except Exception:
                logger.exception(
                    "prewarm sweep could not schedule agent=%s", agent_id
                )
                summary["prewarm_sweep_failures"] += 1
                continue
            if not started:
                # The build from an earlier tick is still running.
                continue
            if counter == "prepared_slots_rebuild_scheduled":
                self._prewarm_rebuild_scheduled_at[agent_id] = now
            summary[counter] += 1
            logger.info(
                "prewarm sweep scheduled a build: agent=%s slot=%s reason=%s",
                agent_id,
                (manifest or {}).get("slot_id") if isinstance(manifest, dict) else None,
                reason,
            )
        return summary

    async def reap_abandoned_agent_boxes(
        self, *, limit: int = 100
    ) -> dict[str, int]:
        """Reclaim Agent-resident sandboxes with no confirmed occupants.

        Session cleanup may end without invoking termination. Check sessions,
        startup allocations, young admissions, and prepared slots before
        destroying a sandbox, using the same occupancy checks as termination.
        """

        from astrabox.persistence.repository.agent_repository import (
            AgentRepository,
        )

        summary = {
            "agent_boxes_scanned": 0,
            "agent_boxes_reaped": 0,
            "agent_boxes_kept": 0,
            "agent_box_reap_failures": 0,
        }
        repo = AgentRepository()
        try:
            rows = await repo.list_agents_with_resident_boxes(limit=limit)
        except Exception:
            logger.exception("agent-box reap could not list resident boxes")
            summary["agent_box_reap_failures"] += 1
            return summary
        for row in rows:
            agent_id = str((row or {}).get("agent_id") or "").strip()
            sandbox_id = str((row or {}).get("sandbox_id") or "").strip()
            if not agent_id or not sandbox_id:
                continue
            summary["agent_boxes_scanned"] += 1
            try:
                row_backend = str(
                    (row or {}).get("sandbox_backend") or ""
                ).strip().lower()
                if row_backend:
                    # The row itself is the backend authority here: sixty-six
                    # dangling pointers made destroy_sandbox_by_id's owner
                    # lookup fail forever ("no session or agent row owns it")
                    # while the pointer sat on the very row being swept.
                    probe = None
                    with contextlib.suppress(Exception):
                        probe = await sandbox_for_name(row_backend).probe(
                            sandbox_id
                        )
                    if (
                        probe is not None
                        and probe.probe_status
                        == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND
                    ):
                        await repo.compare_and_update_agent(
                            agent_id,
                            expected={"sandbox_id": sandbox_id},
                            updates={
                                "sandbox_id": None,
                                "sandbox_backend": None,
                                "_resident_sandbox_generation": None,
                            },
                        )
                        summary["agent_boxes_reaped"] += 1
                        logger.info(
                            "agent-box reap: cleared dangling pointer "
                            "agent=%s box=%s (control plane no longer knows "
                            "it)",
                            agent_id,
                            sandbox_id,
                        )
                        continue
                if await self.agent_box_has_other_occupants(
                    sandbox_id, excluding=""
                ):
                    summary["agent_boxes_kept"] += 1
                    continue
                destruction = await self.destroy_sandbox_by_id(sandbox_id)
                if not destruction.confirmed:
                    logger.warning(
                        "agent-box reap could not confirm destruction "
                        "agent=%s box=%s: %s",
                        agent_id,
                        sandbox_id,
                        destruction.detail,
                    )
                    summary["agent_box_reap_failures"] += 1
                    continue
                cleared = await repo.compare_and_update_agent(
                    agent_id,
                    expected={"sandbox_id": sandbox_id},
                    updates={
                        "sandbox_id": None,
                        "sandbox_backend": None,
                        "_resident_sandbox_generation": None,
                    },
                )
                if not cleared:
                    # A concurrent update points the row at another sandbox;
                    # preserve that binding while completing this destruction.
                    logger.info(
                        "agent-box reap: row moved during reap agent=%s "
                        "box=%s",
                        agent_id,
                        sandbox_id,
                    )
                summary["agent_boxes_reaped"] += 1
                logger.info(
                    "agent-box reap: gave back empty box agent=%s box=%s",
                    agent_id,
                    sandbox_id,
                )
            except Exception:
                logger.exception(
                    "agent-box reap failed agent=%s box=%s",
                    agent_id,
                    sandbox_id,
                )
                summary["agent_box_reap_failures"] += 1
        return summary

    async def reconcile_startup_allocations(
        self,
        *,
        stale_before: datetime,
        limit: int = 50,
    ) -> dict[str, int]:
        """Adopt READY allocations and release abandoned startup resources.

        Candidate rows are hints. Each row is re-read before acting so a worker
        that publishes READY after the scan cannot have its live sandbox reaped
        by this reconciler. Recent CREATING rows remain owned by their worker;
        every other unadopted allocation is released through the same scope-aware
        cleanup used by an ordinary start failure.
        """

        candidates = await self._sessions_repo.list_startup_allocation_candidates(
            limit=limit
        )
        summary = {
            "startup_allocation_candidates": len(candidates),
            "startup_allocations_adopted": 0,
            "startup_allocations_released": 0,
            "startup_allocations_deferred": 0,
            "startup_allocation_failures": 0,
        }
        for candidate in candidates:
            session_id = str((candidate or {}).get("session_id") or "").strip()
            if not session_id:
                summary["startup_allocation_failures"] += 1
                continue
            current = await self._sessions_repo.get_session_including_deleted(session_id)
            for retained in (current or {}).get("_retained_startup_allocations", ()):
                try:
                    allocation = SandboxAllocation.from_record(retained["allocation"])
                    # A rejected owner can have handed shared compute to a
                    # successor. Do not infer destruction authority from its
                    # Session label: only an absent supplier box can be forgotten.
                    if await self._allocation_box_is_gone(allocation):
                        await self._sessions_repo.clear_retained_startup_allocation(
                            session_id, retained
                        )
                        self._forget_pending_allocation(session_id, allocation)
                        summary["startup_allocations_released"] += 1
                    else:
                        summary["startup_allocations_deferred"] += 1
                        logger.warning(
                            "retained startup allocation requires scoped cleanup session=%s receipt=%s",
                            session_id,
                            retained,
                        )
                except Exception:
                    summary["startup_allocation_failures"] += 1
                    logger.exception(
                        "retained startup allocation reconciliation failed session=%s receipt=%s",
                        session_id,
                        retained,
                    )
            raw = (current or {}).get("startup_allocation")
            if not isinstance(current, dict) or not isinstance(raw, dict):
                continue
            try:
                allocation = SandboxAllocation.from_record(raw)
            except ValueError:
                summary["startup_allocation_failures"] += 1
                logger.exception(
                    "startup allocation reconcile found invalid record session=%s",
                    session_id,
                )
                continue

            state = str(current.get("state") or "").strip()
            bound_sandbox_id = str(current.get("sandbox_id") or "").strip()
            if (
                not bool(current.get("deleted"))
                and state == SessionState.READY.value
                and bound_sandbox_id == allocation.sandbox_id
            ):
                try:
                    await self._complete_startup_allocation(
                        session_id,
                        allocation,
                    )
                except Exception:
                    summary["startup_allocation_failures"] += 1
                    logger.exception(
                        "startup allocation adoption failed session=%s sandbox=%s",
                        session_id,
                        allocation.sandbox_id,
                    )
                else:
                    summary["startup_allocations_adopted"] += 1
                continue

            if (
                not bool(current.get("deleted"))
                and state == SessionState.CREATING.value
                and allocation
                in self._pending_startup_allocations.get(session_id, ())
            ):
                summary["startup_allocations_deferred"] += 1
                continue

            updated_at = str(current.get("updated_at") or "").strip()
            updated = None
            if updated_at:
                with contextlib.suppress(TypeError, ValueError):
                    updated = parse_iso(updated_at)
            if (
                not bool(current.get("deleted"))
                and
                state == SessionState.CREATING.value
                and updated is not None
                and updated > stale_before
            ):
                summary["startup_allocations_deferred"] += 1
                continue

            # A terminal Session proves execution ended, not that cleanup
            # succeeded. Keep its exact scope until the same supplier release
            # path used during startup confirms it, including isolated ids in
            # a shared box that must remain alive for other conversations.
            try:
                cleanup = await self.cleanup_startup_allocation(session_id)
            except BaseException:
                summary["startup_allocation_failures"] += 1
                logger.exception(
                    "startup allocation reconcile raised session=%s sandbox=%s",
                    session_id,
                    allocation.sandbox_id,
                )
                continue
            if cleanup.released and cleanup.record_cleared:
                summary["startup_allocations_released"] += 1
            else:
                summary["startup_allocation_failures"] += 1
                logger.error(
                    "startup allocation reconcile retained session=%s sandbox=%s "
                    "detail=%s",
                    session_id,
                    allocation.sandbox_id,
                    cleanup.detail,
                )
        return summary

    #: Maximum startup-allocation records read by the occupancy check, ordered
    #: by newest update. This bounded page can include retained cleanup records.
    _STARTUP_ALLOCATIONS_IN_FLIGHT_CEILING = 500

    async def agent_box_has_other_occupants(
        self,
        sandbox_id: str,
        *,
        excluding: str,
        provider: SandboxProvider | None = None,
    ) -> bool:
        """Check for occupants other than ``excluding`` before destroying a box.

        Check bound Sessions, startup allocations, prepared slots and recent
        admissions because a joining conversation may not have a Session
        binding yet. The provider's live-session census also catches occupants
        not visible in those records.

        A missing sandbox id or a lookup exception retains the box. A negative
        census result contributes no occupancy evidence; the ownership records
        determine the result in that case.
        """

        target = str(sandbox_id or "").strip()
        if not target:
            return True
        try:
            bound = await self._sessions_repo.list_sessions_by_sandbox_id(target)
            for row in bound:
                if str((row or {}).get("session_id") or "").strip() != excluding:
                    return True
            # Read a larger page than the repository default to include more
            # concurrent startups. The repository orders recently updated
            # allocation records first so recent joiners are considered first.
            starting = await self._sessions_repo.list_startup_allocation_candidates(
                limit=self._STARTUP_ALLOCATIONS_IN_FLIGHT_CEILING
            )
            for row in starting:
                if str((row or {}).get("session_id") or "").strip() == excluding:
                    continue
                allocation = (row or {}).get("startup_allocation")
                if not isinstance(allocation, dict):
                    continue
                if str(allocation.get("sandbox_id") or "").strip() == target:
                    return True
            # A prepared slot occupies the box without being a session at all:
            # its whole purpose is to exist BEFORE a conversation claims it, so
            # it is recorded on the Agent row and no session query can see it.
            # Destroying the box under one throws away the prepared unit the next
            # conversation was going to claim, and the loss shows up as a claim
            # miss with nothing pointing back here.
            from astrabox.core.service.orchestrator.agent.prepared_slots import (
                PREPARED_SLOT_FIELD,
            )
            from astrabox.persistence.repository.agent_repository import (
                AgentRepository,
            )

            # place_in_agent_box records admission before placement completes
            # and startup_allocation is written. Consult that ledger to retain
            # a box while a joiner is absent from the allocation records.
            # young_admissions excludes entries past the ledger's grace period.
            from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
                young_admissions,
            )

            agent_repo = AgentRepository()
            agent_rows: dict[str, dict[str, Any]] = {}
            # A full box can stop being the Agent's preferred destination while
            # a prepared slot still lives in it. The Session being released is
            # the durable link back to that Agent; reading it directly avoids
            # mistaking "not the current preferred box" for "not owned".
            read_departing = getattr(
                self._sessions_repo, "get_session_including_deleted", None
            )
            if excluding and callable(read_departing):
                departing = await read_departing(excluding)
                departing_agent_id = str(
                    (departing or {}).get("agent_id") or ""
                ).strip()
                if departing_agent_id:
                    departing_agent = await agent_repo.get_agent(departing_agent_id)
                    if isinstance(departing_agent, dict):
                        agent_rows[departing_agent_id] = departing_agent
            for agent in await agent_repo.list_agents_by_sandbox_id(target):
                agent_id = str((agent or {}).get("agent_id") or "").strip()
                if agent_id:
                    agent_rows[agent_id] = agent
            for agent in agent_rows.values():
                manifest = (agent or {}).get(PREPARED_SLOT_FIELD)
                if isinstance(manifest, dict) and (
                    str(manifest.get("sandbox_id") or "").strip() == target
                ):
                    return True
                for admission in young_admissions(agent, target):
                    admitted = str(admission.get("session_id") or "").strip()
                    if admitted != excluding:
                        return True
            # The provider census checks live isolated sessions directly,
            # including a prepared-slot claimant whose ownership records have
            # not yet caught up with its placement.
            # Inventory-driven callers already have the provider. Requiring an
            # owner lookup here would prevent examining an ownerless resource.
            if provider is None:
                provider = await self.resolve_sandbox_provider(target)
            census = getattr(provider, "count_live_isolated_sessions", None)
            if callable(census):
                live = await census(target)
                if live < 0:
                    # An unavailable census does not add an occupant. The
                    # ownership checks above determine whether to retain the
                    # box; the caller must separately confirm its destruction.
                    logger.info(
                        "terminate: box=%s did not answer the session census; "
                        "leaving the decision to its owner records",
                        target,
                    )
                # Every caller reaches this question after the departing
                # placement has closed, or without a departing placement at
                # all. Any active isolated session therefore belongs to an
                # occupant that still needs the box.
                if live > 0:
                    logger.info(
                        "terminate: box=%s still holds %d isolated sessions; "
                        "keeping it",
                        target,
                        live,
                    )
                    return True
        except Exception:
            logger.warning(
                "terminate could not establish whether box=%s still has "
                "occupants; keeping it",
                target,
                exc_info=True,
            )
            return True
        logger.info(
            "terminate: box=%s has no conversation, no joiner and no prepared "
            "slot left in it; destroying rather than holding it for its lease",
            target,
        )
        return False

    async def _release_runtime_placement(self, runtime: Any, *, session_id: str) -> bool:
        """Close a finished conversation's isolated session inside a shared box.

        Failing to close it is not fatal to the caller — the conversation is over
        either way — but it is loud, because an unclosed session keeps a uid, a
        home and a listening runner alive in somebody else's box.
        """
        agent = getattr(runtime, "agent", None)
        inner = getattr(agent, "_inner", agent)
        release = getattr(inner, "release_placement", None)
        if not callable(release):
            # Engines whose SessionRuntime carries no executor handle (the
            # box-service engines publish agent=None) still ran in a real
            # isolated session; close it through the same row-driven path a
            # post-restart terminate uses, rather than declaring the session
            # unclosable and refusing the whole delete.
            try:
                released = await self._release_persisted_placement(
                    session_id=session_id,
                    sandbox_id=str(runtime.sandbox_id or ""),
                )
            except Exception as exc:
                logger.error(
                    "terminate session=%s could not close isolated session %s "
                    "in box=%s: %s",
                    session_id,
                    runtime.isolated_session_id,
                    runtime.sandbox_id,
                    exc,
                )
                return False
            if released is not None:
                return True
            logger.error(
                "terminate session=%s ran in isolated session %s but neither "
                "its runtime nor its row can close it; the session stays open "
                "in box=%s",
                session_id,
                runtime.isolated_session_id,
                runtime.sandbox_id,
            )
            return False
        try:
            await release()
        except Exception as exc:
            logger.error(
                "terminate session=%s could not close isolated session %s in box=%s: %s",
                session_id,
                runtime.isolated_session_id,
                runtime.sandbox_id,
                exc,
            )
            return False
        else:
            logger.info(
                "terminate closed the conversation's session session=%s iso=%s "
                "box=%s (the box stays for the other conversations)",
                session_id,
                runtime.isolated_session_id,
                runtime.sandbox_id,
            )
            return True

    async def _rollback_runtime_start(
        self,
        session_id: str,
    ) -> tuple[str | None, str | None]:
        """Release the resource an adapter allocated before publishing a runtime.

        The adapter may close its own half-built transport, but it never decides
        what sandbox resource it owns or how to release it.  A failed cleanup
        remains durably named and is returned as structured error evidence.
        """

        ownership = current_recovery(session_id)
        if ownership is not None:
            await ownership.require_current()
        try:
            cleanup = await self._cleanup_pending_startup_allocations(session_id)
        except BaseException as exc:
            pending_ids = self._pending_startup_sandbox_ids(session_id)
            leaked = pending_ids[0] if len(pending_ids) == 1 else None
            detail = (
                "platform startup rollback raised "
                f"{type(exc).__name__}: {exc}"
            )
            logger.error(
                "%s session=%s pending=%s",
                detail,
                session_id,
                pending_ids,
            )
            return leaked, detail

        destruction = cleanup.destruction
        if cleanup.released and cleanup.record_cleared:
            return None, None
        if cleanup.released:
            return None, cleanup.detail or "startup allocation record was not cleared"
        if (
            destruction is not None
            and destruction.outcome == SANDBOX_DESTRUCTION_NOTHING_NAMED
        ):
            return None, None
        detail = cleanup.detail or (
            destruction.detail if destruction is not None else "cleanup made no decision"
        )
        logger.error(
            "platform startup rollback incomplete session=%s sandbox=%s detail=%s",
            session_id,
            cleanup.leaked_sandbox_id or "<shared-allocation>",
            detail,
        )
        return cleanup.leaked_sandbox_id, detail

    @staticmethod
    def _runtime_start_error(
        *,
        engine_kind: str,
        original: BaseException,
        leaked_sandbox_id: str | None,
        cleanup_error: str | None,
    ) -> APIError:
        suffixes: list[str] = []
        data: dict[str, Any] = {}
        if isinstance(original, APIError) and isinstance(original.data, dict):
            data.update(original.data)
        if leaked_sandbox_id:
            data["sandbox_id"] = leaked_sandbox_id
            data["leaked_sandbox_id"] = leaked_sandbox_id
            suffixes.append(f"leaked_sandbox_id={leaked_sandbox_id}")
        if cleanup_error:
            data["cleanup_error"] = cleanup_error
            suffixes.append(cleanup_error)
        suffix = f"; {'; '.join(suffixes)}" if suffixes else ""

        if isinstance(original, APIError):
            return APIError(
                code=original.code,
                message=f"{original.message}{suffix}",
                status_code=original.status_code,
                data=data or None,
                category=original.category,
                retryable=original.retryable,
                user_message=original.user_message,
                debug_message=original.debug_message,
                evidence=original.evidence,
                cause_code=original.cause_code,
            )
        return APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"engine={engine_kind} runtime failed: {original}{suffix}",
            status_code=502,
            data=data or None,
        )

    async def _start_runtime(
        self,
        session_id: str,
        template: AgentView,
        *,
        assignment_id: str,
        user_id: str | None = None,
        permission_mode: str | None = None,
        progress_callback: ProgressCallback = None,
        callback_url: str | None = None,
        workspace_plan: RuntimeWorkspacePlan,
    ) -> SessionRuntime:
        adapter = get_engine_adapter(workspace_plan.engine_kind)
        from astrabox.core.service.orchestrator.engine.startup import (
            start_platform_runtime,
        )
        from astrabox.core.service.orchestrator.agent.prepared_slots import (
            _counted_agent_start,
        )

        try:
            with _counted_agent_start(str(getattr(template, "agent_id", "") or "")):
                return await start_platform_runtime(
                    self,
                    adapter,
                    session_id=session_id,
                    assignment_id=assignment_id,
                    template=template,
                    workspace_plan=workspace_plan,
                    user_id=user_id,
                    permission_mode=permission_mode,
                    progress_callback=progress_callback,
                    callback_url=callback_url,
                )
        except asyncio.CancelledError:
            if not self._quiesced_reason:
                await self._rollback_runtime_start(session_id)
            else:
                logger.warning(
                    "runtime start cancelled during shutdown; durable allocation "
                    "left for bootstrap recovery session=%s reason=%s",
                    session_id,
                    self._quiesced_reason,
                )
            raise
        except BaseException as exc:
            leaked_sandbox_id, cleanup_error = await self._rollback_runtime_start(
                session_id
            )
            wrapped = self._runtime_start_error(
                engine_kind=workspace_plan.engine_kind,
                original=exc,
                leaked_sandbox_id=leaked_sandbox_id,
                cleanup_error=cleanup_error,
            )
            raise wrapped from exc

    # ═════════════════════════════════════════════════════════════════════════
    # EnginePlatform — the public engine-facing surface.
    #
    # Contract: astrabox/core/service/orchestrator/engine/platform.py.
    # Engine adapters and turn transports call only the methods below; the
    # underscore implementations above stay internal to this class and may be
    # reorganized freely. Conformance is pinned by
    # tests/engine_platform_conformance_test.py.
    # ═════════════════════════════════════════════════════════════════════════

    @property
    def deployment_settings(self) -> Any:
        """Read-only deployment settings (EnginePlatform tier 1)."""
        return self._settings

    def resolve_model_access(self, mc: dict[str, Any]) -> ResolvedModelAccess:
        """Resolve one engine-neutral model access value (EnginePlatform tier 1)."""
        return self._resolve_model_access(mc)

    def resolve_sandbox_backend_secret(
        self,
        template: Any,
        *,
        backend: str | None = None,
    ) -> str:
        """Sandbox backend secret material for *template* (EnginePlatform tier 1)."""
        return self._resolve_sandbox_backend_secret(template, backend=backend)

    async def resolve_session_egress_credentials(
        self,
        session_id: str,
        *,
        placeholder_context: str | None = None,
    ) -> list[Any]:
        """The session's outbound vault credentials (EnginePlatform tier 1).

        Resolved on every call rather than cached with the session: the
        secret may rotate between calls, while the placeholder remains stable
        for one private sandbox generation. That lets the runner and later
        isolated terminal address the same proxy-side Vault entry without
        putting the real value in either process environment. Recovery mints a
        new generation and therefore a new placeholder.
        """
        # Imported here, like the seam lookups elsewhere in this module: the
        # vault service pulls in the secret store, and the manager must stay
        # importable by anything that only needs its types.
        from astrabox.core.service.orchestrator.vault_service import VaultService

        target = str(session_id or "").strip()
        if not target:
            return []
        session = await SessionRepository().get_session(target)
        vault_ids = (session or {}).get("vault_ids")
        if not isinstance(vault_ids, list) or not vault_ids:
            return []
        sandbox_generation = str((session or {}).get("sandbox_generation") or "").strip()
        if not sandbox_generation:
            raise APIError(
                code="SANDBOX_CREDENTIAL_CONTEXT_UNAVAILABLE",
                message="the session is missing its private sandbox generation",
                status_code=409,
            )
        context = str(placeholder_context or "").strip()
        return await VaultService().resolve_egress_credentials(
            [str(v) for v in vault_ids if str(v or "").strip()],
            placeholder_context=context or f"{target}:{sandbox_generation}",
        )

    async def resolve_session_mcp_credentials(
        self,
        session_id: str,
        server_urls: list[str],
    ) -> MCPOutboundCredentialResolution:
        """The Session's MCP scope and current egress-side credentials."""

        from astrabox.core.service.orchestrator.vault_service import VaultService

        target = str(session_id or "").strip()
        session = await SessionRepository().get_session(target) if target else None
        if not isinstance(session, dict):
            raise APIError(
                code="SANDBOX_CREDENTIAL_CONTEXT_UNAVAILABLE",
                message="the session credential context is unavailable",
                status_code=409,
            )
        vault_ids = session.get("vault_ids")
        if not isinstance(vault_ids, list):
            raise APIError(
                code="SANDBOX_CREDENTIAL_CONTEXT_UNAVAILABLE",
                message="the session credential context is malformed",
                status_code=409,
            )
        normalized_vault_ids = [
            str(v).strip() for v in vault_ids if str(v or "").strip()
        ]
        from astrabox.core.service.orchestrator.runtime.mcp_credentials import (
            mcp_vault_scope_id,
        )

        scope_id = mcp_vault_scope_id(normalized_vault_ids)
        credentials = (
            await VaultService().resolve_mcp_credentials(
                normalized_vault_ids,
                list(server_urls),
            )
            if server_urls and normalized_vault_ids
            else []
        )
        return MCPOutboundCredentialResolution(
            scope_id=scope_id,
            credentials=tuple(credentials),
        )

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
        *,
        replaces: SandboxAllocation | None = None,
    ) -> None:
        """Persist a startup-owned resource (EnginePlatform tier 1)."""

        await self._record_startup_allocation(session_id, allocation, replaces=replaces)

    async def record_attached_runtime_identity(
        self,
        session_id: str,
        *,
        sandbox_id: str,
        runtime_identity: dict[str, Any],
    ) -> None:
        """Persist a reattached placement without crossing a newer box binding."""

        target_session = str(session_id or "").strip()
        target_sandbox = str(sandbox_id or "").strip()
        identity = dict(runtime_identity)
        ownership = current_recovery(target_session)
        expected = {"sandbox_id": target_sandbox}
        if ownership is not None:
            expected.update(ownership.expected)
        recorded = await SessionRepository().compare_and_update_session(
            target_session,
            expected=expected,
            updates={"runtime_identity": identity},
        )
        if recorded:
            return
        latest = await SessionRepository().get_session(target_session)
        if (
            isinstance(latest, dict)
            and str(latest.get("sandbox_id") or "").strip() == target_sandbox
            and latest.get("runtime_identity") == identity
            and all(latest.get(key) == value for key, value in expected.items())
        ):
            return
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"runtime identity for Session {target_session!r} could not be "
                "recorded without crossing a newer sandbox binding"
            ),
            status_code=409,
        )

    def forget_published_startup_allocation(
        self,
        session_id: str,
        *,
        sandbox_id: str,
    ) -> None:
        """Drop the process-local name after the READY write adopted it."""

        self._forget_pending_sandbox(session_id, sandbox_id)

    def register_terminal_execution(
        self,
        execution_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        """Bind an in-process streamed terminal execution for interruption."""

        target = str(execution_id or "").strip()
        if not target or not is_isolated_terminal_execution_id(target):
            return
        self._terminal_execution_tasks[target] = task

    def unregister_terminal_execution(
        self,
        execution_id: str,
        task: asyncio.Task[Any] | None,
    ) -> None:
        """Drop a binding only when it still points at this exact run."""

        target = str(execution_id or "").strip()
        current = self._terminal_execution_tasks.get(target)
        if current is not None and (task is None or current is task):
            self._terminal_execution_tasks.pop(target, None)

    async def _interrupt_isolated_terminal_execution(self, execution_id: str) -> None:
        task = self._terminal_execution_tasks.get(execution_id)
        if task is None or task.done():
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="interrupt failed: isolated terminal stream is not owned by this worker",
                status_code=409,
            )
        task.cancel("terminal interrupted")
        # Hand control to the cancelled task so it can close the HTTP stream,
        # delete its disposable terminal session and create the replacement.
        # Do not await the owner here: its supervising worker settles it and
        # waiting from this control path would create a cycle.
        await asyncio.sleep(0)

    async def clone_default_repo(
        self,
        sandbox: Any,
        template: "AgentView",
        target_cwd: str,
        session_id: str,
        *,
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        """Clone the template's default repo into the workspace (EnginePlatform tier 1)."""
        await self._clone_default_repo(
            sandbox,
            template,
            target_cwd,
            session_id,
            runtime_identity=runtime_identity,
        )

    async def mount_assistant_workspace_storage(
        self,
        sandbox: Any,
        *,
        user_id: str,
        assistant_id: str,
        engine_kind: str,
    ) -> None:
        """Mount the persistent Assistant workspace (EnginePlatform tier 1)."""

        await self._mount_assistant_workspace_storage(
            sandbox,
            user_id=user_id,
            assistant_id=assistant_id,
            engine_kind=engine_kind,
        )

    async def prepare_workspace_storage(
        self,
        sandbox: Any,
        *,
        workspace_ref: WorkspaceRef,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        """Materialize workspace files without exposing the medium to engines."""

        await storage_provider().prepare(
            workspace_ref,
            box=sandbox,
            box_path=box_path,
            owner=owner,
            group=group,
        )

    async def resolve_runtime_sandbox_backend(
        self,
        session_id: str,
        *,
        workspace_plan: Any,
    ) -> str:
        """EnginePlatform tier 1: the backend name comes only from the persisted row.

        The template's ``sandbox_backend`` is a mutable authoring-time default;
        a live runtime must reconnect to whatever backend actually holds its
        sandbox.  The persisted row is that record, so it is the only source
        read here.
        """
        subject_kind = str(getattr(workspace_plan, "subject_kind", "") or "").strip()
        if subject_kind == "assistant_runtime":
            assistant_id = str(getattr(workspace_plan, "assistant_id", "") or "").strip()
            lookup_user_id = str(getattr(workspace_plan, "user_id", "") or "").strip()
            if not assistant_id or not lookup_user_id:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox backend resolution failed",
                    status_code=502,
                    debug_message="assistant runtime authority is incomplete",
                )
            row = await AssistantWorkspaceRepository().get_workspace(
                lookup_user_id,
                assistant_id,
            )
            if not isinstance(row, dict):
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox backend resolution failed",
                    status_code=502,
                    debug_message="assistant runtime authority is unavailable",
                )
            backend = str(row.get("sandbox_backend") or "").strip().lower()
            if not backend:
                # The materialized workspace intentionally starts unbound. Its
                # first hidden bootstrap session already carries the persisted
                # backend selected at admission; workspace claim atomically
                # installs that value. No template/request value participates.
                bootstrap = await SessionRepository().get_session(session_id)
                backend = str((bootstrap or {}).get("sandbox_backend") or "").strip().lower()
            if not backend:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox backend resolution failed",
                    status_code=502,
                    debug_message="assistant runtime has no persisted backend",
                )
            return backend

        row = await SessionRepository().get_session(session_id)
        if not isinstance(row, dict):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox backend resolution failed",
                status_code=502,
                debug_message="session runtime authority is unavailable",
            )
        backend = str(row.get("sandbox_backend") or "").strip().lower()
        if not backend:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox backend resolution failed",
                status_code=502,
                debug_message="session has no persisted backend",
            )
        return backend


    @staticmethod
    def _require_runtime_engine_manifest(
        runtime: SessionRuntime,
    ) -> EngineCapabilityManifest:
        try:
            return bound_engine_client_manifest(runtime)
        except TypeError as exc:
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=str(exc),
                status_code=502,
            ) from exc
