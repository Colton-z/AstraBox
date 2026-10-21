"""Connect a session to its runtime by attaching to the box it is bound to.

The reader host's connector asks :meth:`RuntimeEnsure.ensure_runtime` for a
session's live, conversation-bound runtime. It resolves one across
agent_chat and assistant_chat sessions: a cached runtime, a lightweight
reattach to the bound box, or a full prepare when that fails. A box found
gone is reported (``sandbox_gone``), never replaced here: the reader host
ends what it took and starts a replacement only for waiting inputs. The
attach itself belongs to the engine adapter; this module owns the
orchestration around it and the session write-backs.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from typing import Any

from astrabox.persistence.repository.session_repository import SessionRepository
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
from astrabox.seams.runtime_preparation import TURN_PREPARATION_USER_ACTION_REQUIRED
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.runtime.execution_target import require_execution_target

logger = get_logger(__name__)

_RUNTIME_ENSURE_ATTACHED = "ATTACHED"
_RUNTIME_ENSURE_BINDING_MISSING = "BINDING_MISSING"
_RUNTIME_ENSURE_ATTACH_FAILED = "ATTACH_FAILED"


class _RuntimeEnsureResult:
    """What one ensure attempt produced: a runtime, or why there is none."""

    __slots__ = ("status", "runtime", "error_text", "session", "sandbox_gone")

    def __init__(
        self,
        *,
        status: str,
        runtime: Any | None = None,
        error_text: str | None = None,
        session: dict[str, Any] | None = None,
        sandbox_gone: bool = False,
    ) -> None:
        self.status = status
        self.runtime = runtime
        self.error_text = error_text
        self.session = session
        # The bound box is gone, so the reader host replaces it instead of
        # reconnecting to the same dead instance.
        self.sandbox_gone = sandbox_gone


def _is_sandbox_gone_error(exc: BaseException) -> bool:
    """Whether an attach failed because the bound box is gone.

    The sandbox ingress answers 404 once the instance behind its endpoint is
    gone, and the runtime manager raises that as ``SANDBOX_GONE``. On the
    ``open_sandbox`` backend a box whose Pod was deleted out of band keeps a
    Ready record and a stale endpoint, so the only evidence is that the
    address stopped accepting connections, which that provider also raises as
    ``SANDBOX_GONE``. This predicate therefore treats "gone" and "unreachable
    for the whole of one attach" alike: being wrong costs one replacement
    box, while the conversation keeps its transcript; the other way round,
    the conversation would stay wedged.
    """
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_sandbox_gone_error(sub) for sub in exc.exceptions)
    if str(getattr(exc, "code", "") or "").strip() == "SANDBOX_GONE":
        return True
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict) and isinstance(exc, APIError) and isinstance(exc.data, dict):
        payload = exc.data
    if isinstance(payload, dict) and str(payload.get("code") or "").strip() == "SANDBOX_GONE":
        return True
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return _is_sandbox_gone_error(cause)
    return False


class RuntimeEnsure:
    """Resolve an attached, usable SessionRuntime for a session.

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
        assistant_workspace_service: Any | None = None,
        sandbox_lifecycle_service: Any,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._sessions_repo = sessions_repo
        self._agent_config = agent_config
        self._agent_repo = agent_repo
        self._assistant_workspace_service = assistant_workspace_service
        self._sandbox_lifecycle_service = sandbox_lifecycle_service

    async def _ensure_lightweight_runtime_for_turn(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        engine_session_key: str | None,
        workspace_plan: Any,
        session_kind: str,
        runtime_identity: dict[str, Any] | None,
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
            user_id=str(session.get("user_id") or "").strip() or None,
            engine_session_key=engine_session_key,
            permission_mode=str(session.get("permission_mode") or "").strip() or None,
            session_kind=effective_kind,
            workspace_plan=workspace_plan,
            runtime_identity=runtime_identity,
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
                resume_engine_session_key=engine_session_key,
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
        """Whether the backend found the box's data plane gone (false Ready).

        Only a ``DEAD`` assessment converges the box's owners; ``UNKNOWN`` is no
        evidence, and the attach judges the box as before.
        """

        from astrabox.seams.sandbox import SANDBOX_DATA_PLANE_DEAD, sandbox_for_name

        backend = str(session.get("sandbox_backend") or "").strip().lower()
        if not backend:
            return False
        assessment = await sandbox_for_name(backend).assess_data_plane(sandbox_id)
        if assessment.state != SANDBOX_DATA_PLANE_DEAD:
            logger.debug(
                "data-plane assessment sandbox=%s: %s %s",
                sandbox_id,
                assessment.state,
                assessment.detail,
            )
        return assessment.state == SANDBOX_DATA_PLANE_DEAD


    async def _attach_runtime_for_turn(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        engine_session_key: str | None,
        workspace_plan: Any,
        session_kind: str,
        runtime_identity: dict[str, Any] | None,
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
                runtime_identity=runtime_identity,
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
            # The bound sandbox is gone (killed or reclaimed while its lease was
            # still in the future). Its effective lease is over, so expires_at is
            # stamped now beside runtime_unavailable.
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
                    "%s found the sandbox gone session=%s sandbox=%s",
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
            return _RuntimeEnsureResult(
                status=_RUNTIME_ENSURE_ATTACH_FAILED,
                error_text="agent_chat session has no bound sandbox",
                session=session,
                sandbox_gone=True,
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
            runtime_identity=runtime_identity,
            log_context="agent_chat turn prepare",
        )
        # A gone box is reported, not replaced here: the reader host ends what
        # the box took with it and starts a replacement only for waiting inputs.
        return attach_result

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
        try:
            await self._runtime_manager.wake_parked_session(session)
        except Exception as exc:
            logger.warning(
                "agent_chat turn prepare: resume of parked sandbox errored "
                "session=%s sandbox=%s parked_at=%s: %s",
                session_id, sandbox_id, parked_at, exc,
            )
            return
        logger.info(
            "agent_chat turn prepare: resumed parked sandbox session=%s sandbox=%s "
            "parked_at=%s",
            session_id, sandbox_id, parked_at,
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

    async def ensure_runtime(
        self,
        session: dict[str, Any],
        *,
        user: UserContext | None = None,
    ) -> _RuntimeEnsureResult:
        require_execution_target(session)
        session_id = str(session["session_id"])
        session_kind = require_session_kind(session.get("session_kind"))
        if session_kind == "agent_chat":
            return await self._prepare_agent_chat_turn_runtime(
                session,
                user=user,
            )
        assistant_binding_result = await self._prepare_assistant_chat_turn_binding(session)
        if assistant_binding_result is not None:
            return assistant_binding_result

        runtime = self._runtime_manager.get_runtime(
            session_id,
            sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
        )
        if runtime is not None:
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
                runtime_identity=runtime_identity,
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
            _raise_user_action_refusal(lightweight_exc, session_id)
            try:
                if isinstance(lightweight_exc, APIError) and not lightweight_exc.retryable:
                    raise lightweight_exc
                logger.warning(
                    "engine turn transport lightweight attach failed, escalating to full prepare "
                    "engine=%s session=%s sandbox=%s: %s",
                    workspace_plan.engine_kind,
                    session_id,
                    sandbox_id,
                    lightweight_exc,
                )
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
                _raise_user_action_refusal(full_exc, session_id)
                error_text = str(full_exc)
                logger.warning(
                    "engine turn transport attach failed "
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


def _raise_user_action_refusal(exc: BaseException, session_id: str) -> None:
    """Refuse this message for the user's prerequisite; the runtime is not at fault.

    The box and engine are healthy; only this Session's materials wait on the
    user. So the attach is not escalated and the runtime is not marked
    unavailable, and the refusal carries the action the console shows.
    """
    if isinstance(exc, APIError) and exc.code == TURN_PREPARATION_USER_ACTION_REQUIRED:
        raise APIError(
            code=exc.code, message=exc.message, status_code=409,
            data={"session_id": session_id}, evidence=exc.evidence,
        ) from exc
