"""Runtime ensure / turn-attach orchestration (the ``RuntimeEnsure`` collaborator).

Resolves an attached, usable SessionRuntime for a turn across agent_chat and
assistant_chat session kinds, including the sandbox-gone re-borrow path. The
attach itself belongs to the engine adapter's turn transport
(``attach_runtime``); this collaborator owns the orchestration around it —
permission preflight, session write-backs, and the gone-sandbox re-borrow.

Constructed once in ``TurnService.__init__``, holding ``runtime_manager``,
``sessions_repo``, ``agent_config``, ``agent_repo``, ``permission_lifecycle``
and ``assistant_workspace_service``.

Callers outside this file hold the ``TurnService``, not this collaborator, so
``_ensure_runtime_for_session``, ``_ensure_runtime_lightweight_for_session``
and ``acquire_engine_control_runtime`` are also exposed as thin delegators
there (turn_service.py); the session-kernel mixins reach them by that path.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from astrabox.persistence.repository import SessionRepository
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.runtime.conversation_identity import normalize_runtime_identity
from astrabox.core.service.orchestrator.runtime_binding import (
    is_assistant_user_conversation,
    reconcile_session_runtime_binding,
)
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycle,
)
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind

# Imports from the same cycle-breaking leaf module turn_service.py uses (see
# that module's docstring); the underscore aliases match the names the call
# sites below expect.
from astrabox.core.service.orchestrator.stream_errors import (
    RUNTIME_ENSURE_ATTACHED as _RUNTIME_ENSURE_ATTACHED,
    RUNTIME_ENSURE_ATTACH_FAILED as _RUNTIME_ENSURE_ATTACH_FAILED,
    RUNTIME_ENSURE_BINDING_MISSING as _RUNTIME_ENSURE_BINDING_MISSING,
    RuntimeEnsureResult as _RuntimeEnsureResult,
    is_recoverable_turn_attach_error as _is_recoverable_turn_attach_error,
    is_sandbox_gone_error as _is_sandbox_gone_error,
)

logger = get_logger(__name__)


class RuntimeEnsure:
    """Resolve an attached, usable SessionRuntime for a turn.

    See the module docstring for the responsibilities this collaborator owns
    and how its methods are reached.
    """

    def __init__(
        self,
        *,
        runtime_manager: RemoteAgentRuntimeManager,
        sessions_repo: SessionRepository,
        agent_config: AgentConfigService,
        agent_repo=None,
        permission_lifecycle: PermissionLifecycle,
        assistant_workspace_service: Any | None = None,
        sandbox_lifecycle_service: Any,
        has_turn_dispatch_permission_context: Callable[..., bool],
    ) -> None:
        self._runtime_manager = runtime_manager
        self._sessions_repo = sessions_repo
        self._agent_config = agent_config
        self._agent_repo = agent_repo
        self._permission_lifecycle = permission_lifecycle
        self._assistant_workspace_service = assistant_workspace_service
        self._sandbox_lifecycle_service = sandbox_lifecycle_service
        # Pure, stateless interaction-dispatch predicate defined on TurnService
        # and injected by reference so there is exactly one implementation.
        self._has_turn_dispatch_permission_context = has_turn_dispatch_permission_context

    async def _ensure_permission_before_turn_dispatch(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        turn_id: str | None,
        command_id: str | None,
        requested_permission_mode: str | None,
        runtime: Any | None,
    ) -> None:
        if not self._has_turn_dispatch_permission_context(
            turn_id=turn_id,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
        ):
            return
        await self._permission_lifecycle.ensure_before_turn_dispatch(
            session_id=session_id,
            session=session,
            turn_id=turn_id,
            command_id=command_id,
            requested_mode=requested_permission_mode,
            runtime=runtime,
        )

    async def _ensure_lightweight_runtime_for_turn(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        engine_session_key: str | None,
        workspace_plan: Any,
        session_kind: str,
        template_name: str | None,
        runtime_identity: dict[str, Any] | None,
        turn_id: str | None,
        command_id: str | None,
        requested_permission_mode: str | None,
    ) -> tuple[Any, dict[str, Any]]:
        session_id = str(session["session_id"])
        effective_kind = require_session_kind(session_kind or session.get("session_kind"))
        if effective_kind == "agent_chat" and runtime_identity is None:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="agent_chat runtime_identity is required",
                status_code=409,
            )
        template = await self._agent_config.resolve_session_harness(session)
        if template is None:
            raise APIError(
                code="TEMPLATE_NOT_ALLOWED",
                message="agent not found",
                status_code=403,
            )

        runtime = await self._runtime_manager.ensure_runtime_lightweight(
            session_id,
            template,
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            permission_mode=str(session.get("permission_mode") or "").strip() or None,
            session_kind=effective_kind,
            workspace_plan=workspace_plan,
            runtime_identity=runtime_identity,
        )
        await self._ensure_permission_before_turn_dispatch(
            session_id=session_id,
            session=session,
            turn_id=turn_id,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
            runtime=runtime,
        )
        return runtime, {
            "runtime_unavailable": False,
            "last_error": None,
            "engine_session_key": getattr(runtime, "engine_session_key", None),
        }

    def _plan_runtime_attach_for_turn(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
        session_kind: str,
        sandbox_id: str,
        engine_session_key: str | None,
        runtime_identity: dict[str, Any] | None,
    ) -> Any:
        workspace_ref = session.get("workspace_ref")
        if (
            session_kind == "assistant_chat"
            and isinstance(workspace_ref, dict)
            and str(workspace_ref.get("kind") or "").strip() == "assistant"
        ):
            engine_kind = resolve_session_engine_kind(session)
            user_id = str(workspace_ref.get("user_id") or "").strip()
            assistant_id = str(workspace_ref.get("assistant_id") or "").strip()
            if not user_id or not assistant_id:
                raise APIError(
                    code="WORKSPACE_PLAN_INVALID",
                    message=(
                        "assistant_chat attach requires workspace_ref "
                        "user_id and assistant_id"
                    ),
                    status_code=500,
                )
            return self._runtime_manager.plan_assistant_runtime_attach(
                user_id=user_id,
                assistant_id=assistant_id,
                runtime_key=session_id,
                sandbox_id=sandbox_id,
                existing_terminal_cwd=str(session.get("terminal_cwd") or "").strip() or None,
                engine_kind=engine_kind,
            )
        return self._runtime_manager.plan_runtime_attach(
            agent_id=str(session.get("agent_id") or ""),
            session_id=session_id,
            session_kind=session_kind,
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            existing_terminal_cwd=str(session.get("terminal_cwd") or "").strip() or None,
            engine_kind=resolve_session_engine_kind(session),
            runtime_identity=runtime_identity,
        )

    async def _sandbox_data_plane_dead(
        self, session: dict[str, Any], sandbox_id: str
    ) -> bool:
        """Ask the backend whether the box's data plane is gone (false Ready)."""

        from astrabox.seams.sandbox import sandbox_for_name

        backend = str(session.get("sandbox_backend") or "").strip().lower()
        if not backend:
            return False
        provider = sandbox_for_name(backend)
        assess = getattr(provider, "data_plane_unreachable", None)
        if not callable(assess):
            return False
        return bool(await assess(sandbox_id))

    async def _attach_runtime_for_turn(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        engine_session_key: str | None,
        workspace_plan: Any,
        session_kind: str,
        template_name: str | None,
        runtime_identity: dict[str, Any] | None,
        turn_id: str | None,
        command_id: str | None,
        requested_permission_mode: str | None,
        log_context: str,
    ) -> _RuntimeEnsureResult:
        """Attach the engine transport's runtime for a turn, or fail durably.

        The attach itself is entirely the engine transport's
        (``attach_runtime`` via ``ensure_runtime_lightweight``); this wrapper
        owns only the session write-back on both outcomes and the
        sandbox-gone classification the caller's re-borrow path keys on.
        """
        session_id = str(session["session_id"])
        # One attach path for every backend: the engine transport's
        # ``attach_runtime``. There is no separate dispatch-register round trip
        # to branch on. The try/except is the point of this wrapper — a
        # SANDBOX_GONE from the attach must land in the durable write-back
        # below, because a parked box that died out-of-band reaches a turn
        # exactly here, and the lapsed-lease re-borrow keys on that
        # classification.
        try:
            runtime, write_back = await self._ensure_lightweight_runtime_for_turn(
                session=session,
                sandbox_id=sandbox_id,
                engine_session_key=engine_session_key,
                workspace_plan=workspace_plan,
                session_kind=session_kind,
                template_name=template_name,
                runtime_identity=runtime_identity,
                turn_id=turn_id,
                command_id=command_id,
                requested_permission_mode=requested_permission_mode,
            )
        except Exception as exc:
            error_text = str(exc)
            logger.warning(
                "%s failed session=%s sandbox=%s: %s",
                log_context,
                session_id,
                sandbox_id,
                exc,
            )
            gone = _is_sandbox_gone_error(exc)
            if not gone and sandbox_id:
                # A transport timeout cannot distinguish an unreachable sandbox
                # from a dead one with a stale Ready record. Probe the data plane
                # before classifying it as dead; uncertainty preserves the binding.
                with contextlib.suppress(Exception):
                    gone = await self._sandbox_data_plane_dead(
                        session, sandbox_id
                    )
                if gone:
                    logger.warning(
                        "%s assessed sandbox dead by data-plane probe "
                        "session=%s sandbox=%s",
                        log_context,
                        session_id,
                        sandbox_id,
                    )
            # The bound sandbox is gone (out-of-band kill / reclaim while the lease is
            # still nominally in the future, so the pre-dispatch expiry gate can't catch
            # it). Stamp expires_at to now in addition to the durable runtime_unavailable
            # flag: the rendered runtime_unavailable is derived and a still-READY
            # runtime_binding masks the row flag, so the lapsed-lease gate (which reads
            # expires_at straight off the row) is what reliably re-borrows on the next
            # message. A gone sandbox's effective lease is over.
            failure_write: dict[str, Any] = {
                "runtime_unavailable": True,
                "last_error": error_text,
            }
            if gone:
                failure_write["expires_at"] = utcnow_iso()
            with contextlib.suppress(Exception):
                await self._sessions_repo.update_session(session_id, failure_write)
            session.update(failure_write)
            if gone:
                last_error = str(getattr(exc, "message", "") or error_text)
                try:
                    await self._sandbox_lifecycle_service.converge_dead_sandbox_owners(
                        sandbox_id,
                        last_error=last_error,
                        reason="runtime_attach:SANDBOX_GONE",
                    )
                except Exception:
                    logger.exception(
                        "%s could not converge owners of confirmed-dead sandbox "
                        "session=%s sandbox=%s",
                        log_context,
                        session_id,
                        sandbox_id,
                    )
                logger.warning(
                    "%s sandbox gone — armed lapsed-lease re-borrow session=%s sandbox=%s",
                    log_context,
                    session_id,
                    sandbox_id,
                )
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text=error_text,
                session=session,
                sandbox_gone=gone,
            )
        with contextlib.suppress(Exception):
            await self._sessions_repo.update_session(session_id, write_back)
        session.update(write_back)
        return _RuntimeEnsureResult(
            status=_RUNTIME_ENSURE_ATTACHED,
            runtime=runtime,
            session=session,
        )

    async def _prepare_agent_chat_turn_runtime(
        self,
        session: dict[str, Any],
        *,
        user: UserContext | None = None,
        turn_id: str | None = None,
        command_id: str | None = None,
        requested_permission_mode: str | None = None,
    ) -> _RuntimeEnsureResult:
        """Prepare only the missing runtime layer for a mutating agent_chat turn."""

        session_id = str(session["session_id"])
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))
        if runtime_identity is None:
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text="agent_chat runtime_identity is required",
                session=session,
            )
        if self._agent_repo is None:
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text="agent repository is required for agent_chat turn runtime prepare",
                session=session,
            )

        agent_id = str(session.get("agent_id") or "").strip()
        if not agent_id:
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text="agent_chat session missing agent_id",
                session=session,
            )

        # The Session owns this runtime allocation and records its resolved
        # sandbox binding. The provider decides whether that binding names a
        # dedicated box or an isolated placement in the Agent's shared box. A
        # missing/dead binding falls through to the same attach-or-recreate path.
        sandbox_id = str(session.get("sandbox_id") or "").strip()

        runtime = self._runtime_manager.get_runtime(
            session_id,
            sandbox_id=sandbox_id or None,
        )
        if runtime is not None:
            await self._ensure_permission_before_turn_dispatch(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                requested_permission_mode=requested_permission_mode,
                runtime=runtime,
            )
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACHED,
                runtime=runtime,
                session=session,
            )

        sandbox_id = str(session.get("sandbox_id") or "").strip()
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))
        if runtime_identity is None:
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text="agent_chat runtime_identity is required",
                session=session,
            )
        if not sandbox_id:
            return await self._reborrow_agent_chat_runtime_for_turn(
                session=session,
                agent_id=agent_id,
                engine_session_key=str(session.get("engine_session_key") or "").strip() or None,
                runtime_identity=runtime_identity,
                turn_id=turn_id,
                command_id=command_id,
                requested_permission_mode=requested_permission_mode,
            )

        engine_session_key = str(session.get("engine_session_key") or "").strip() or None
        workspace_plan = self._runtime_manager.plan_runtime_attach(
            agent_id=agent_id,
            session_id=session_id,
            session_kind="agent_chat",
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            existing_terminal_cwd=str(session.get("terminal_cwd") or "").strip() or None,
            engine_kind=resolve_session_engine_kind(session),
            runtime_identity=runtime_identity,
        )

        # OpenSandbox park/resume: a paused box must be woken before any
        # attach can reach its runner.
        await self._wake_parked_sandbox(
            session_id=session_id,
            session=session,
            sandbox_id=sandbox_id,
        )

        attach_result = await self._attach_runtime_for_turn(
            session=session,
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            workspace_plan=workspace_plan,
            session_kind="agent_chat",
            template_name=str(session.get("template_name") or "").strip(),
            runtime_identity=runtime_identity,
            turn_id=turn_id,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
            log_context="agent_chat turn prepare",
        )
        if not getattr(attach_result, "sandbox_gone", False):
            return attach_result
        # The bound sandbox is gone (out-of-band kill / reclaim). Borrow a fresh
        # sandbox and resume the existing Claude session by id: the externalized
        # mirror jsonl plus the Agent SDK restore full context on load, so only the
        # user's message needs to be re-dispatched. ensure_runtime performs this as
        # an in-place runtime swap, keeping the in-flight turn's checkpoint valid so
        # the message is delivered in the same send.
        return await self._reborrow_agent_chat_runtime_for_turn(
            session=session,
            agent_id=agent_id,
            engine_session_key=engine_session_key,
            runtime_identity=runtime_identity,
            turn_id=turn_id,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
        )

    async def _wake_parked_sandbox(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        sandbox_id: str,
    ) -> None:
        """Resume a box the idle sweeper parked, before anything tries to reach it.

        A parked box is current on disk and absent from the wire: its compute was
        freed, so the attach that follows would fail exactly the way an unreachable
        box fails, and the gone-sandbox path would then cold-create over files that
        are still there. Waking first is what makes a conversation picked up a day
        later continue in its own box.

        The mark is this session's own record; the control plane remains the
        authority. A box this mark names as parked can have been destroyed since —
        its retention ran out — and a box whose image moved on can refuse the
        resumed handshake. Both land here as a resume that
        does not come back RUNNING, and both fall through to the ordinary
        gone-sandbox re-borrow, with the workspace loss named in the log instead of
        showing up later as missing files. The mark is cleared only on a resume that
        took: a transient control-plane failure must leave the one record that says
        those files are still reachable.
        """
        parked_at = str((session or {}).get("sandbox_parked_at") or "").strip()
        if not parked_at:
            return
        resumed = False
        try:
            resumed = await self._runtime_manager.resume_sandbox_by_id(sandbox_id)
        except Exception as exc:
            logger.warning(
                "agent_chat turn prepare: resume of parked sandbox errored "
                "session=%s sandbox=%s parked_at=%s: %s",
                session_id, sandbox_id, parked_at, exc,
            )
            return
        if not resumed:
            logger.warning(
                "agent_chat turn prepare: parked sandbox did not resume — this "
                "turn continues on a FRESH box and that workspace is not in it "
                "session=%s sandbox=%s parked_at=%s",
                session_id, sandbox_id, parked_at,
            )
            return
        # The box kept its id and its files; it did not keep its address. A resume
        # reschedules the workload, so the endpoint stored before the pause names a
        # place nothing answers at any more, and the attach below prefers a stored
        # endpoint over resolving one. Clearing it is what sends this turn back to
        # the control plane for the box's current address.
        #
        # Without this clear the failure is silent on both sides: the resumed box
        # is healthy on its own `:8000` and its log records no host request at
        # all, because the host spends the whole attempt talking to the pre-pause
        # address and never reaches it. `sandbox_endpoint` is the
        # field paired with `sandbox_id` to name a box (they are cleared together
        # when one dies, see ``terminal_session_updates``); a resume keeps the id
        # and must drop the address.
        session["sandbox_parked_at"] = None
        session["sandbox_endpoint"] = None
        with contextlib.suppress(Exception):
            await self._sessions_repo.update_session(
                session_id,
                {"sandbox_parked_at": None, "sandbox_endpoint": None},
                touch_updated_at=False,
            )
        logger.info(
            "agent_chat turn prepare: resumed parked sandbox session=%s sandbox=%s "
            "parked_at=%s",
            session_id, sandbox_id, parked_at,
        )

    async def _reborrow_agent_chat_runtime_for_turn(
        self,
        *,
        session: dict[str, Any],
        agent_id: str,
        engine_session_key: str | None,
        runtime_identity: dict[str, Any] | None,
        turn_id: str | None,
        command_id: str | None,
        requested_permission_mode: str | None,
    ) -> _RuntimeEnsureResult:
        """Take a fresh sandbox when the durable binding is absent or gone.

        A new sandbox is created and Claude Code is started
        resuming ``engine_session_key`` (mirror jsonl + Agent SDK reconstruct the
        conversation context on load). The session row's sandbox pointer is
        swapped to the fresh sandbox and the turn re-dispatches the user's message
        on it, all within the same send, without a lifecycle recover/reset, so the
        in-flight turn checkpoint is never disturbed.
        """
        session_id = str(session["session_id"])
        replaced_sandbox_id = str(session.get("sandbox_id") or "").strip()
        template = await self._agent_config.resolve_session_harness(session)
        if template is None:
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text=f"agent configuration not found for sandbox re-borrow (session={session_id})",
                session=session,
            )
        logger.warning(
            "agent_chat sandbox %s — borrowing fresh sandbox + resuming session=%s agent=%s resume=%s",
            "gone" if replaced_sandbox_id else "binding absent",
            session_id,
            agent_id,
            bool(engine_session_key),
        )
        # ``ensure_runtime(..., sandbox_id=None)`` selects the create branch only
        # after the resident cache is empty.  Retire the exact runtime that still
        # names the confirmed-dead box; a concurrently installed replacement has
        # a different id and is deliberately left alone.
        resident = self._runtime_manager.get_runtime(session_id)
        if (
            replaced_sandbox_id
            and resident is not None
            and str(getattr(resident, "sandbox_id", "") or "").strip().lower()
            == replaced_sandbox_id.lower()
        ):
            await self._runtime_manager.evict_runtime_if_current(session_id, resident)
        start_plan = self._runtime_manager.plan_agent_chat_runtime_start(
            session_id=session_id,
            agent_id=agent_id,
            template=template,
            resume_engine_session_key=engine_session_key,
            existing_terminal_cwd=str(session.get("terminal_cwd") or "").strip() or None,
            # This is the rebuild path — the box is gone and the conversation
            # needs another. What it must not get is another POSIX owner: its
            # account outlives the box on a shared one, and asking for a
            # different uid fails the bootstrap's check against it.
            runtime_identity=(
                session.get("runtime_identity")
                if isinstance(session.get("runtime_identity"), dict)
                else None
            ),
        )
        try:
            runtime = await self._runtime_manager.ensure_runtime(
                session_id,
                template,
                sandbox_id=None,
                assignment_id=command_id,
                engine_session_key=engine_session_key,
                permission_mode=str(session.get("permission_mode") or "").strip() or None,
                session_kind="agent_chat",
                workspace_plan=start_plan,
                runtime_identity=runtime_identity,
            )
        except Exception as exc:
            error_text = str(exc)
            logger.warning(
                "agent_chat sandbox re-borrow failed session=%s: %s", session_id, error_text
            )
            # Leave the durable compute-gone signal armed so the next message
            # recovers through the entry gate.
            with contextlib.suppress(Exception):
                await self._sessions_repo.update_session(
                    session_id,
                    {"runtime_unavailable": True, "last_error": error_text, "expires_at": utcnow_iso()},
                )
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text=error_text,
                session=session,
                sandbox_gone=True,
            )
        new_sandbox_id = str(getattr(runtime, "sandbox_id", "") or "").strip()
        if not new_sandbox_id or (
            replaced_sandbox_id
            and new_sandbox_id.lower() == replaced_sandbox_id.lower()
        ):
            error_text = (
                "rebuilt sandbox must differ from the reclaimed sandbox "
                if replaced_sandbox_id
                else "rebuilt sandbox must have an identity "
            ) + (
                f"(old={replaced_sandbox_id or '<none>'}, "
                f"returned={new_sandbox_id or '<missing>'})"
            )
            # Do not publish the ambiguous binding, and do not destroy the box:
            # an identity collision is not authority to touch what may now be a
            # different tenant's resource.  The process-local runtime holds the exact
            # client handle whose retirement prevents the next request from silently
            # reusing it.
            with contextlib.suppress(Exception):
                await self._runtime_manager.evict_runtime_if_current(session_id, runtime)
            failure_write = {
                "runtime_unavailable": True,
                "last_error": error_text,
                "expires_at": utcnow_iso(),
            }
            with contextlib.suppress(Exception):
                await self._sessions_repo.update_session(session_id, failure_write)
            session.update(failure_write)
            logger.error(
                "agent_chat sandbox replacement returned invalid identity "
                "session=%s old_sandbox=%s",
                session_id,
                replaced_sandbox_id or "<none>",
            )
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text=error_text,
                session=session,
                sandbox_gone=True,
            )
        write_back: dict[str, Any] = {
            "sandbox_id": new_sandbox_id or None,
            "runtime_unavailable": False,
            "last_error": None,
            # A freshly borrowed box is not parked. The mark belongs to the box the
            # pointer just stopped naming, and leaving it would both send the next
            # turn to resume a running box and keep this session out of the idle
            # sweep's candidate set for good.
            "sandbox_parked_at": None,
            "engine_session_key": getattr(runtime, "engine_session_key", None) or engine_session_key,
        }
        # Refresh the durable lease so the entry gate does not re-fire on the stale
        # (now-overwritten) past expires_at the gone-detection stamped.
        with contextlib.suppress(Exception):
            real_exp = await self._runtime_manager.get_sandbox_expires_at(new_sandbox_id)
            if real_exp is not None and hasattr(real_exp, "isoformat"):
                write_back["expires_at"] = real_exp.isoformat()
        new_identity = getattr(runtime, "runtime_identity", None)
        if isinstance(new_identity, dict) and new_identity:
            write_back["runtime_identity"] = new_identity
        new_cwd = str(getattr(runtime, "terminal_cwd", "") or "").strip()
        if new_cwd:
            write_back["terminal_cwd"] = new_cwd
        updated = await self._sessions_repo.update_session(session_id, write_back)
        if updated is False:
            raise RuntimeError(
                f"rebuilt Session runtime write-back did not match session={session_id}"
            )
        session.update(write_back)
        await self._sandbox_lifecycle_service.project_session_runtime_ready(
            session,
            reason=f"agent_chat_reborrow_ready:{new_sandbox_id}",
        )
        await self._ensure_permission_before_turn_dispatch(
            session_id=session_id,
            session=session,
            turn_id=turn_id,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
            runtime=runtime,
        )
        logger.warning(
            "agent_chat sandbox re-borrow ready session=%s new_sandbox=%s", session_id, new_sandbox_id
        )
        return _RuntimeEnsureResult(
            status=_RUNTIME_ENSURE_ATTACHED,
            runtime=runtime,
            session=session,
        )

    async def _prepare_assistant_chat_turn_binding(
        self,
        session: dict[str, Any],
    ) -> _RuntimeEnsureResult | None:
        if not is_assistant_user_conversation(session):
            return None
        if self._assistant_workspace_service is None:
            if str(session.get("state") or "").strip() == "TERMINATED":
                return _RuntimeEnsureResult(
                    status=_RUNTIME_ENSURE_ATTACH_FAILED,
                    error_text="assistant workspace service unavailable; cannot resolve binding for terminated session",
                    session=session,
                )
            return None
        session_id = str(session.get("session_id") or "").strip()
        try:
            reconciled, resolution = await reconcile_session_runtime_binding(
                session=session,
                sessions_repo=self._sessions_repo,
                agent_repo=self._agent_repo,
                assistant_workspace_service=self._assistant_workspace_service,
                persist=True,
            )
        except Exception as exc:
            error_text = str(exc)
            with contextlib.suppress(Exception):
                await self._sessions_repo.update_session(
                    session_id,
                    {"runtime_unavailable": True, "last_error": error_text},
                )
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text=error_text,
                session=session,
            )

        session.clear()
        session.update(reconciled)
        if resolution.can_dispatch:
            return None
        error_text = (
            resolution.reason_message
            or resolution.reason_code
            or "assistant workspace runtime binding is not ready"
        )
        with contextlib.suppress(Exception):
            await self._sessions_repo.update_session(
                session_id,
                {"runtime_unavailable": True, "last_error": error_text},
            )
        return _RuntimeEnsureResult(
            status=_RUNTIME_ENSURE_BINDING_MISSING,
            error_text=error_text,
            session=session,
        )

    async def _ensure_runtime_for_session(
        self,
        session: dict[str, Any],
        *,
        user: UserContext | None = None,
        turn_id: str | None = None,
        command_id: str | None = None,
        requested_permission_mode: str | None = None,
    ) -> _RuntimeEnsureResult:
        session_id = str(session["session_id"])
        session_kind = require_session_kind(session.get("session_kind"))
        if session_kind == "agent_chat":
            return await self._prepare_agent_chat_turn_runtime(
                session,
                user=user,
                turn_id=turn_id,
                command_id=command_id,
                requested_permission_mode=requested_permission_mode,
            )
        assistant_binding_result = await self._prepare_assistant_chat_turn_binding(session)
        if assistant_binding_result is not None:
            return assistant_binding_result

        runtime = self._runtime_manager.get_runtime(
            session_id,
            sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
        )
        if runtime is not None:
            if self._has_turn_dispatch_permission_context(
                turn_id=turn_id,
                command_id=command_id,
                requested_permission_mode=requested_permission_mode,
            ):
                await self._permission_lifecycle.ensure_before_turn_dispatch(
                    session_id=session_id,
                    session=session,
                    turn_id=turn_id,
                    command_id=command_id,
                    requested_mode=requested_permission_mode,
                    runtime=runtime,
                )
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACHED,
                runtime=runtime,
                session=session,
            )

        sandbox_id = str(session.get("sandbox_id") or "").strip()
        session_kind = require_session_kind(session.get("session_kind"))
        if not sandbox_id:
            logger.warning(
                "cannot ensure runtime: missing sandbox_id session=%s kind=%s",
                session_id,
                session_kind,
            )
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text="runtime sandbox_id is missing; recover session before sending turns",
                session=session,
            )

        engine_session_key = str(session.get("engine_session_key") or "").strip() or None
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))
        workspace_plan = self._plan_runtime_attach_for_turn(
            session=session,
            session_id=session_id,
            session_kind=session_kind,
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            runtime_identity=runtime_identity,
        )

        try:
            runtime, write_back = await self._ensure_lightweight_runtime_for_turn(
                session=session,
                sandbox_id=sandbox_id,
                engine_session_key=engine_session_key,
                workspace_plan=workspace_plan,
                session_kind=session_kind,
                template_name=str(session.get("template_name") or "").strip(),
                runtime_identity=runtime_identity,
                turn_id=turn_id,
                command_id=command_id,
                requested_permission_mode=requested_permission_mode,
            )
            with contextlib.suppress(Exception):
                await self._sessions_repo.update_session(session_id, write_back)
            session.update(write_back)
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACHED,
                runtime=runtime,
                session=session,
            )
        except Exception as lightweight_exc:
            logger.warning(
                "engine turn transport lightweight attach failed, escalating to full prepare "
                "engine=%s session=%s sandbox=%s: %s",
                workspace_plan.engine_kind,
                session_id,
                sandbox_id,
                lightweight_exc,
            )
            try:
                template = await self._agent_config.resolve_session_harness(session)
                if template is None:
                    raise lightweight_exc
                runtime = await self._runtime_manager.ensure_runtime(
                    session_id,
                    template,
                    sandbox_id=sandbox_id,
                    engine_session_key=engine_session_key,
                    permission_mode=str(session.get("permission_mode") or "").strip() or None,
                    session_kind=session_kind,
                    workspace_plan=workspace_plan,
                    runtime_identity=runtime_identity,
                )
                write_back: dict[str, Any] = {
                    "runtime_unavailable": False,
                    "last_error": None,
                    "engine_session_key": getattr(runtime, "engine_session_key", None),
                }
                with contextlib.suppress(Exception):
                    await self._sessions_repo.update_session(session_id, write_back)
                session.update(write_back)
                return _RuntimeEnsureResult(
                    status=_RUNTIME_ENSURE_ATTACHED,
                    runtime=runtime,
                    session=session,
                )
            except Exception as full_exc:
                error_text = str(full_exc)
                logger.warning(
                    "engine turn transport full prepare also failed "
                    "engine=%s session=%s sandbox=%s: %s",
                    workspace_plan.engine_kind,
                    session_id,
                    sandbox_id,
                    full_exc,
                )
                # A box that the provider says is gone is a state this platform
                # has an answer for, and losing that fact here is what turned it
                # into an unhandled error: the caller received no runtime and no
                # reason, so it could only raise. Both attempts failing for the
                # same absent box is still that state.
                gone = _is_sandbox_gone_error(full_exc)
                failure_write: dict[str, Any] = {
                    "runtime_unavailable": True,
                    "last_error": error_text,
                }
                if gone:
                    # The lapsed-lease gate reads expires_at straight off the
                    # row, and a gone box's effective lease is over however far
                    # in the future it nominally runs. Stamping it is what makes
                    # the next message re-borrow instead of meeting the same
                    # absent box.
                    failure_write["expires_at"] = utcnow_iso()
                with contextlib.suppress(Exception):
                    await self._sessions_repo.update_session(session_id, failure_write)
                session.update(failure_write)
                if gone:
                    try:
                        await self._sandbox_lifecycle_service.converge_dead_sandbox_owners(
                            sandbox_id,
                            last_error=str(
                                getattr(full_exc, "message", "") or error_text
                            ),
                            reason="engine_turn_transport:SANDBOX_GONE",
                        )
                    except Exception:
                        logger.exception(
                            "engine turn transport could not converge owners of a "
                            "confirmed-dead sandbox session=%s sandbox=%s",
                            session_id,
                            sandbox_id,
                        )
                    logger.warning(
                        "engine turn transport sandbox gone — armed lapsed-lease "
                        "re-borrow session=%s sandbox=%s",
                        session_id,
                        sandbox_id,
                    )
                return _RuntimeEnsureResult(
                    status=_RUNTIME_ENSURE_ATTACH_FAILED,
                    error_text=error_text,
                    session=session,
                    sandbox_gone=gone,
                )

    async def acquire_engine_control_runtime(self, session: dict[str, Any]) -> Any:
        """Return a conversation-bound runtime for an engine control operation.

        Interaction answers, turn interrupts, and child controls all need the
        same attachment semantics after a host restart. Process-local cache
        presence is not capability evidence: when the cache is empty, attach
        through the selected adapter's turn transport before exposing the
        control to its caller.
        """

        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message="engine control requires a session_id",
                status_code=409,
            )
        session_kind = require_session_kind(session.get("session_kind"))
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))
        if session_kind == "agent_chat" and runtime_identity is None:
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message=(
                    "engine control cannot attach: agent_chat has no "
                    f"runtime_identity (session={session_id})"
                ),
                status_code=409,
            )

        runtime = self._runtime_manager.get_runtime(
            session_id,
            sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
        )
        if runtime is not None:
            return runtime

        sandbox_id = str(session.get("sandbox_id") or "").strip()
        session_kind = str(session.get("session_kind") or "agent_chat").strip()
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))

        if not sandbox_id:
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message=(
                    "engine control cannot attach without a sandbox "
                    f"(session={session_id})"
                ),
                status_code=409,
            )

        template = await self._agent_config.resolve_session_harness(session)
        if not template:
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message=(
                    "engine control cannot resolve the session's engine "
                    f"configuration (session={session_id})"
                ),
                status_code=409,
            )

        engine_session_key = str(session.get("engine_session_key") or "").strip() or None
        workspace_plan = self._plan_runtime_attach_for_turn(
            session=session,
            session_id=session_id,
            session_kind=session_kind,
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            runtime_identity=runtime_identity,
        )

        logger.info(
            "acquiring engine control runtime session=%s sandbox=%s kind=%s",
            session_id,
            sandbox_id,
            session_kind,
        )
        try:
            return await self._runtime_manager.ensure_runtime_lightweight(
                session_id,
                template,
                sandbox_id=sandbox_id,
                engine_session_key=engine_session_key,
                permission_mode=str(session.get("permission_mode") or "").strip()
                or None,
                session_kind=session_kind,
                workspace_plan=workspace_plan,
                runtime_identity=runtime_identity,
            )
        except Exception as exc:
            # A typed SANDBOX_GONE is the transport's verdict that the box did
            # not answer at all, and it is recorded here rather than at any one
            # caller because all five of them need it and none of them owns it.
            # The error is re-raised unchanged: what the caller does about a
            # dead box stays the caller's decision, only the durable fact is
            # this method's business.
            if _is_sandbox_gone_error(exc):
                await self._record_confirmed_dead_sandbox(
                    session,
                    exc=exc,
                    log_context="engine control attach",
                )
            raise

    async def _ensure_runtime_lightweight_for_session(self, session: dict[str, Any]):
        """Best-effort lightweight attach used by recovery probes.

        User-invoked controls call :meth:`acquire_engine_control_runtime`
        directly so an attachment failure remains typed and visible. Recovery
        callers retain their optional probe contract and receive ``None``.

        Optional is about the *runtime*, not about what the attempt learned.
        Returning ``None`` here loses no evidence, because the attach has
        already stamped a confirmed-dead box onto this same ``session`` dict
        before raising.

        That ordering is the whole point for this caller. Engine anchor
        recovery calls this, and when it gets ``None`` it asks
        ``_confirmed_dead_sandbox_reason(session)`` two lines later — which
        answers on ``runtime_unavailable`` and deliberately does NOT treat "no
        runtime and a failed attach" as a verdict. That refusal is right in
        general and blind here: on a pooled backend the lifecycle probe it
        falls back to keeps answering Ready for a sandbox whose Pod was deleted
        out-of-band, so unreachability is the only evidence that will ever
        exist, and without the stamp the turn is never settled.
        """

        try:
            return await self.acquire_engine_control_runtime(session)
        except Exception as exc:
            logger.warning(
                "optional runtime attach failed session=%s sandbox=%s: %s",
                str(session.get("session_id") or "").strip() or "<missing>",
                str(session.get("sandbox_id") or "").strip() or "<missing>",
                exc,
            )
            return None

    async def _record_confirmed_dead_sandbox(
        self,
        session: dict[str, Any],
        *,
        exc: Exception,
        log_context: str,
    ) -> None:
        """Write down that this session's bound sandbox is confirmed gone.

        Only a typed ``SANDBOX_GONE`` reaches here. Every other attach failure
        is left alone on purpose: a transient one must not mark a live session
        unavailable, and this path has no turn behind it to fail loudly to.

        The three writes are one fact from three readers' points of view.
        ``runtime_unavailable`` is what the turn coordinator's box-terminal
        test reads, ``expires_at`` is what the lapsed-lease gate reads to
        re-borrow on the next message (a still-READY ``runtime_binding``
        masks the row flag from it), and the owner convergence is what reaches
        the *other* conversations sharing the same box — under shared tenancy
        one dead box wedges every session placed in it, and only the sandbox
        id connects them.
        """

        session_id = str(session.get("session_id") or "").strip()
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        if not session_id or not sandbox_id:
            return
        error_text = str(exc)
        failure_write: dict[str, Any] = {
            "runtime_unavailable": True,
            "last_error": error_text,
            "expires_at": utcnow_iso(),
        }
        with contextlib.suppress(Exception):
            await self._sessions_repo.update_session(session_id, failure_write)
        session.update(failure_write)
        try:
            await self._sandbox_lifecycle_service.converge_dead_sandbox_owners(
                sandbox_id,
                last_error=str(getattr(exc, "message", "") or error_text),
                reason=f"{log_context}:SANDBOX_GONE",
            )
        except Exception:
            logger.exception(
                "%s could not converge owners of confirmed-dead sandbox "
                "session=%s sandbox=%s",
                log_context,
                session_id,
                sandbox_id,
            )
        logger.warning(
            "%s: sandbox is confirmed gone session=%s sandbox=%s",
            log_context,
            session_id,
            sandbox_id,
        )
