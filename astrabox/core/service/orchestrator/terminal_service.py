"""Terminal command execution in sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import shlex
import uuid
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext

from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    identity_workspace_dir,
    identity_workspace_source_dir,
    normalize_runtime_identity,
)
from astrabox.core.service.orchestrator.runtime_binding import (
    is_assistant_user_conversation,
    reconcile_session_runtime_binding,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    PtyTerminal,
    resolve_execd_endpoint,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import get_underlying_sandbox
from astrabox.core.service.orchestrator.runtime.terminal_execution import (
    new_isolated_terminal_execution_id,
)
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.seams.egress_credentials import workload_placeholder_context
from astrabox.seams.sandbox import sandbox_for_name

logger = get_logger(__name__)

# Module-level terminal cwd cache for sessions whose runtime is not loaded.
_terminal_cwd_cache: dict[str, str] = {}

#: The shell each session is talking to, by AstraBox session id. The shell lives
#: in the box, not here — this only remembers which one, so a second command
#: lands in the same one and `cd` therefore means something. Losing the mapping
#: costs a new shell at the session's configured directory, never an error.
_pty_sessions: dict[str, str] = {}


def forget_terminal_session(session_id: str) -> None:
    """Drop process-local shell metadata after final conversation disposal."""
    normalized = str(session_id or "").strip()
    if not normalized:
        return
    _pty_sessions.pop(normalized, None)
    _terminal_cwd_cache.pop(normalized, None)


async def _pty_session_for(
    terminal: PtyTerminal,
    session_id: str,
    cwd: str | None,
    *,
    envs: dict[str, str] | None,
) -> str:
    """The session's shell, opening one if it has none or its last one is gone."""
    known = _pty_sessions.get(session_id)
    if known and await terminal.session_exists(known):
        return known
    opened = await terminal.open_session(cwd=cwd, envs=envs)
    _pty_sessions[session_id] = opened
    return opened


def _isolated_terminal_code(
    command: str,
    *,
    cwd: str | None,
    identity: dict[str, Any],
    marker: str,
) -> str:
    """Wrap one command for the conversation's stateful isolated bash.

    OpenSandbox already establishes the uid/gid and namespaces when it creates
    the isolation session. This wrapper supplies the matching user-space
    identity (HOME, config/cache paths and PATH), selects the terminal cwd, and
    prints a private status/cwd marker. It never invokes ``runuser``: doing so
    inside a uid-switched isolation session would require privilege the
    workload correctly does not have.
    """

    home = str(identity["home_dir"]).rstrip("/")
    linux_user = str(identity["linux_user"])
    config_dir = str(identity.get("config_dir") or "").rstrip("/")
    config_env_var = str(identity.get("config_env_var") or "").strip()
    if config_env_var and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config_env_var) is None:
        raise ValueError("runtime identity config_env_var is invalid")
    if config_env_var and not config_dir:
        raise ValueError("runtime identity config_env_var requires config_dir")
    temp_dir = str(identity.get("temp_dir") or f"{home}/tmp").rstrip("/")
    local_prefix = f"{home}/.local"
    default_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    path = f"{local_prefix}/bin:{default_path}"
    selected_cwd = str(cwd or identity["workspace_dir"]).strip()
    exports = [
        f"export HOME={shlex.quote(home)}",
        f"export USER={shlex.quote(linux_user)} LOGNAME={shlex.quote(linux_user)}",
        f"export PATH={shlex.quote(path)}",
        f"export NPM_CONFIG_PREFIX={shlex.quote(local_prefix)}",
        f"export TMPDIR={shlex.quote(temp_dir)}",
    ]
    if config_env_var:
        exports.append(f"export {config_env_var}={shlex.quote(config_dir)}")
    return "\n".join(
        (
            *exports,
            # A shell-killing command (`exit 9`, or anything that takes bash
            # with it) never reaches the printf below — and never reaches
            # execd's own end marker either, so the vendor's isolated-run
            # surface reports a read error with no exit code at all
            # (isolated_session_ctrl.go returns before its exit-code capture).
            # bash runs EXIT traps before dying, so this is the one channel
            # that still speaks: the trap prints the same status marker with
            # bash's own exit status, and the ordinary parser downstream picks
            # it up with no second code path. Installed per run, idempotently;
            # it fires only when the shell actually exits.
            (
                "trap "
                + shlex.quote(
                    f"printf '%s:%s:%s\\n' {shlex.quote(marker)} \"$?\" \"$PWD\""
                )
                + " EXIT"
            ),
            f"cd -- {shlex.quote(selected_cwd)}",
            str(command or ""),
            "__astrabox_terminal_status=$?",
            (
                "printf '%s:%s:%s\\n' "
                f"{shlex.quote(marker)} \"$__astrabox_terminal_status\" \"$PWD\""
            ),
            "unset __astrabox_terminal_status",
        )
    )


class TerminalService:
    def __init__(
        self,
        *,
        runtime_manager: RemoteAgentRuntimeManager,
        must_get_owned_session,
        sessions_repo: Any | None = None,
        assistant_workspace_service: Any | None = None,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._must_get_owned_session = must_get_owned_session
        self._sessions_repo = sessions_repo
        self._assistant_workspace_service = assistant_workspace_service

    async def _credential_env(
        self,
        session_id: str,
        *,
        credential_slot_id: str | None = None,
    ) -> dict[str, str]:
        """Opaque Vault placeholders safe to expose to this Session's process."""

        slot_id = str(credential_slot_id or "").strip()
        credentials = await self._runtime_manager.resolve_session_egress_credentials(
            session_id,
            **(
                {"placeholder_context": workload_placeholder_context(slot_id)}
                if slot_id
                else {}
            ),
        )
        return {
            str(credential.secret_name): str(credential.placeholder)
            for credential in credentials
            if str(getattr(credential, "secret_name", "") or "").strip()
            and str(getattr(credential, "placeholder", "") or "").strip()
        }

    async def run_terminal_command(
        self,
        user: UserContext,
        session_id: str,
        command: str,
        cwd: str | None = None,
        on_execution_started: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run a command in the sandbox and yield stdout/stderr/exit events."""
        session = await self._must_get_owned_session(user, session_id)
        if is_assistant_user_conversation(session) and self._assistant_workspace_service is not None:
            session, resolution = await reconcile_session_runtime_binding(
                session=session,
                sessions_repo=self._sessions_repo,
                assistant_workspace_service=self._assistant_workspace_service,
                persist=False,
            )
            if not resolution.can_dispatch:
                raise APIError(
                    code=resolution.reason_code or "ASSISTANT_WORKSPACE_NOT_READY",
                    message=resolution.reason_message or "assistant workspace is not ready",
                    status_code=409,
                )
        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        session_kind = require_session_kind((session or {}).get("session_kind"))
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))
        isolated_session_id = str(
            (runtime_identity or {}).get("isolated_session_id") or ""
        ).strip()
        terminal_isolated_session_id = str(
            (runtime_identity or {}).get("terminal_isolated_session_id") or ""
        ).strip()
        if session_kind == "agent_chat" and runtime_identity is None:
            raise APIError(
                code="CONVERSATION_IDENTITY_REQUIRED",
                message="agent_chat terminal access requires runtime_identity",
                status_code=409,
            )
        runtime = self._runtime_manager.get_runtime(session_id, sandbox_id=sandbox_id)

        sandbox = None
        if runtime is not None:
            runtime_user_id = getattr(runtime, "user_id", None)
            if runtime_user_id and runtime_user_id != user.user_id:
                raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
            if not runtime_user_id:
                runtime.user_id = str(session.get("user_id") or "").strip() or None
        else:
            state = str(session.get("state") or "")
            if state == SessionState.CREATING.value:
                raise APIError(
                    code="SESSION_BUSY",
                    message="session is still creating, please retry",
                    status_code=409,
                )
            if state in {SessionState.TERMINATED.value, SessionState.DELETED.value}:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="session terminated, create a new session",
                    status_code=409,
                )
            if sandbox_id and not isolated_session_id:
                sandbox_only = await self._runtime_manager.connect_sandbox_only(sandbox_id)
            else:
                sandbox_only = None

        engine_session_key = str((session or {}).get("engine_session_key") or "").strip() or None
        identity_cwd = identity_workspace_dir(runtime_identity)

        effective_cwd = str(cwd or "").strip() or identity_cwd
        if runtime is not None:
            if not effective_cwd:
                effective_cwd = str(getattr(runtime, "terminal_cwd", "") or "").strip() or None
            sandbox = runtime.sandbox
            if sandbox is None and runtime.agent is not None:
                try:
                    sandbox = runtime.agent.sandbox()
                except Exception:
                    pass
        else:
            if not effective_cwd:
                effective_cwd = _terminal_cwd_cache.get(session_id)
            if not effective_cwd:
                effective_cwd = self._runtime_manager.resolve_session_terminal_cwd(
                    session_id,
                    sandbox_id=sandbox_id,
                    session_kind=session_kind,
                    engine_session_key=engine_session_key,
                )
            sandbox = sandbox_only
            if not isolated_session_id and sandbox is None:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox not available for terminal access",
                    status_code=409,
                )

        if not isolated_session_id and sandbox is None:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox not available for terminal access",
                status_code=409,
            )

        active_task = asyncio.current_task()
        owns_current_task = False
        if active_task is not None and runtime is not None:
            existing_task = runtime.current_task
            if existing_task is None or existing_task.done():
                runtime.current_task = active_task
                owns_current_task = True

        try:
            yield {
                "type": "ack",
                "session_id": session_id,
                "command": command,
                "working_directory": effective_cwd,
                "at": utcnow_iso(),
            }

            if runtime is not None:
                runtime.current_execution_id = None

            credential_env = await self._credential_env(
                session_id,
                credential_slot_id=str(
                    (runtime_identity or {}).get("credential_slot_id") or ""
                ).strip()
                or None,
            )

            if isolated_session_id:
                if not sandbox_id or runtime_identity is None:
                    yield {
                        "type": "stderr",
                        "text": "Command execution error: isolated terminal placement is incomplete\n",
                    }
                    yield {"type": "exit", "exit_code": 1}
                    return
                async for event in self._run_isolated_terminal(
                    session=session,
                    runtime=runtime,
                    runtime_identity=runtime_identity,
                    session_id=session_id,
                    sandbox_id=sandbox_id,
                    isolated_session_id=isolated_session_id,
                    terminal_isolated_session_id=terminal_isolated_session_id,
                    command=command,
                    effective_cwd=effective_cwd,
                    active_task=active_task,
                    on_execution_started=on_execution_started,
                    credential_env=credential_env,
                ):
                    yield event
                return

            try:
                endpoint = await resolve_execd_endpoint(
                    get_underlying_sandbox(sandbox)
                )
                terminal = PtyTerminal(
                    endpoint.origin,
                    headers=endpoint.headers,
                )
                pty_session_id = await _pty_session_for(
                    terminal,
                    session_id,
                    effective_cwd,
                    envs=credential_env or None,
                )
                if runtime is not None:
                    runtime.current_execution_id = pty_session_id
                if on_execution_started is not None:
                    callback_result = on_execution_started(pty_session_id)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            except Exception as exc:
                yield {"type": "stderr", "text": f"Command execution error: {exc}\n"}
                yield {"type": "exit", "exit_code": 1}
                return

            exit_code = 0
            next_cwd: str | None = None
            try:
                async for event in terminal.run(pty_session_id, command):
                    if event.get("type") == "__done__":
                        exit_code = int(event.get("exit_code") or 0)
                        next_cwd = str(event.get("cwd") or "") or None
                        break
                    yield event
            except Exception as exc:
                # The shell may have gone with its box. Drop the remembered id so
                # the next command opens a new one rather than retrying a corpse.
                _pty_sessions.pop(session_id, None)
                yield {"type": "stderr", "text": f"Command execution error: {exc}\n"}
                yield {"type": "exit", "exit_code": 1}
                return

            resolved_cwd = next_cwd or effective_cwd
            if runtime is not None:
                runtime.terminal_cwd = resolved_cwd or runtime.terminal_cwd
            if resolved_cwd:
                _terminal_cwd_cache[session_id] = resolved_cwd
                # The terminal worker persists this into the session snapshot.
                yield {"type": "__cwd__", "path": resolved_cwd}

            yield {"type": "exit", "exit_code": exit_code}
        finally:
            if runtime is not None:
                if owns_current_task and runtime.current_task is active_task:
                    runtime.current_task = None
                runtime.current_execution_id = None

    async def _run_isolated_terminal(
        self,
        *,
        session: dict[str, Any],
        runtime: Any,
        runtime_identity: dict[str, Any],
        session_id: str,
        sandbox_id: str,
        isolated_session_id: str,
        terminal_isolated_session_id: str,
        command: str,
        effective_cwd: str | None,
        active_task: asyncio.Task[Any] | None,
        on_execution_started: Callable[[str], Awaitable[None] | None] | None,
        credential_env: dict[str, str],
    ) -> AsyncIterator[dict[str, Any]]:
        """Run through a terminal-only OpenSandbox isolation session.

        The Agent runner lives in ``isolated_session_id``. The terminal lives
        in a sibling session with the same uid and workspace, so deleting a
        stuck terminal cannot kill or serialize against the Agent runner.
        """

        backend = str(session.get("sandbox_backend") or "").strip().lower()
        if not backend:
            yield {
                "type": "stderr",
                "text": "Command execution error: sandbox backend is missing\n",
            }
            yield {"type": "exit", "exit_code": 1}
            return

        provider = sandbox_for_name(backend)
        execution_id = new_isolated_terminal_execution_id(session_id)
        marker = f"__ASTRABOX_ISOLATED_TERMINAL_{uuid.uuid4().hex}__"
        marker_re = re.compile(rf"{re.escape(marker)}:(-?\d+):([^\r\n]*)")
        code = _isolated_terminal_code(
            command,
            cwd=effective_cwd,
            identity=runtime_identity,
            marker=marker,
        )
        if active_task is not None:
            self._runtime_manager.register_terminal_execution(
                execution_id,
                active_task,
            )
        exit_code: int | None = None
        next_cwd: str | None = None
        provider_exit_code = 1
        shell_died = False
        target_session_id = str(terminal_isolated_session_id or "").strip()
        try:
            if runtime is not None:
                runtime.current_execution_id = execution_id
            if on_execution_started is not None:
                callback_result = on_execution_started(execution_id)
                if inspect.isawaitable(callback_result):
                    await callback_result
            if not target_session_id or target_session_id == isolated_session_id:
                target_session_id, runtime_identity = (
                    await self._replace_isolated_terminal_session(
                        session=session,
                        runtime=runtime,
                        runtime_identity=runtime_identity,
                        sandbox_id=sandbox_id,
                        agent_isolated_session_id=isolated_session_id,
                        old_terminal_session_id=None,
                        provider=provider,
                    )
                )

            for attempt in range(2):
                try:
                    stream_kwargs: dict[str, Any] = {
                        "code": code,
                        # Match the ordinary PTY terminal: a command runs until
                        # it exits or the user interrupts it.
                        "timeout_s": None,
                    }
                    if credential_env:
                        # Only opaque Vault placeholders cross into the run.
                        # Real values remain in OpenSandbox's egress sidecar.
                        stream_kwargs["envs"] = credential_env
                    async for event in provider.stream_in_isolated_session(
                        sandbox_id,
                        target_session_id,
                        **stream_kwargs,
                    ):
                        event_type = str(event.get("type") or "")
                        if event_type == "__done__":
                            provider_exit_code = int(event.get("exit_code") or 0)
                            continue
                        if event_type == "__error__":
                            # The vendor's isolated run failed at the surface —
                            # for a shell-killing command that is EXPECTED (the
                            # run's own end marker died with bash), and the
                            # trap-carried status marker above is the truth.
                            # Render the error for the user either way; what it
                            # additionally means is that this persistent shell
                            # is gone, so replace it now rather than letting
                            # the NEXT command fail on a corpse.
                            shell_died = True
                            yield {
                                "type": "stderr",
                                "text": (
                                    f"{event.get('name') or 'RuntimeError'}: "
                                    f"{event.get('value') or 'isolated run failed'}\n"
                                ),
                            }
                            continue
                        if event_type != "stdout":
                            yield event
                            continue

                        text = str(event.get("text") or "")
                        found = marker_re.search(text)
                        if found is None:
                            if text:
                                yield {"type": "stdout", "text": text}
                            continue
                        exit_code = int(found.group(1))
                        next_cwd = found.group(2).strip() or None
                        before = text[: found.start()]
                        after = text[found.end() :]
                        if before:
                            yield {"type": "stdout", "text": before}
                        if after.strip("\r\n"):
                            yield {"type": "stdout", "text": after}
                    break
                except APIError as exc:
                    if attempt or int(getattr(exc, "status_code", 0) or 0) not in (
                        404,
                        409,
                    ):
                        raise
                    target_session_id, runtime_identity = (
                        await self._replace_isolated_terminal_session(
                            session=session,
                            runtime=runtime,
                            runtime_identity=runtime_identity,
                            sandbox_id=sandbox_id,
                            agent_isolated_session_id=isolated_session_id,
                            old_terminal_session_id=target_session_id,
                            provider=provider,
                        )
                    )

            if exit_code is None:
                # The marker is the line after the command and is printed
                # unconditionally, so its absence means the shell did not reach
                # the end of the script — `exit` at the prompt is the ordinary
                # way that happens. That shell is gone; leaving it named as the
                # conversation's terminal makes every later command fail with
                # `session process has exited`. An interrupt already answers a
                # dead shell by replacing it, and this is the same fact.
                yield {
                    "type": "stderr",
                    "text": "astrabox: the shell exited; starting a new one\n",
                }
                try:
                    target_session_id, runtime_identity = (
                        await self._replace_isolated_terminal_session(
                            session=session,
                            runtime=runtime,
                            runtime_identity=runtime_identity,
                            sandbox_id=sandbox_id,
                            agent_isolated_session_id=isolated_session_id,
                            old_terminal_session_id=target_session_id,
                            provider=provider,
                        )
                    )
                except Exception as exc:
                    # Say it. A terminal that silently stays dead is the defect
                    # this replaces, and a failure here reproduces it exactly.
                    logger.error(
                        "isolated terminal replacement after shell exit failed "
                        "session=%s box=%s: %s",
                        session_id,
                        sandbox_id,
                        exc,
                    )
                    yield {
                        "type": "stderr",
                        "text": (
                            "astrabox: could not start a new shell for this "
                            f"terminal: {exc}\n"
                        ),
                    }
        except asyncio.CancelledError as cancelled:
            # Closing an OpenSandbox SSE stream is not a reliable reusable-shell
            # interrupt. Delete only the disposable terminal session, then
            # create and persist its replacement before the interrupt returns.
            replacement = asyncio.create_task(
                self._replace_isolated_terminal_session(
                    session=session,
                    runtime=runtime,
                    runtime_identity=runtime_identity,
                    sandbox_id=sandbox_id,
                    agent_isolated_session_id=isolated_session_id,
                    old_terminal_session_id=target_session_id,
                    provider=provider,
                )
            )
            try:
                await asyncio.shield(replacement)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await replacement
            except Exception as exc:
                logger.error(
                    "isolated terminal replacement failed session=%s box=%s: %s",
                    session_id,
                    sandbox_id,
                    exc,
                )
            raise cancelled
        except Exception as exc:
            yield {"type": "stderr", "text": f"Command execution error: {exc}\n"}
            yield {"type": "exit", "exit_code": 1}
            return
        finally:
            self._runtime_manager.unregister_terminal_execution(
                execution_id,
                active_task,
            )
            if runtime is not None and runtime.current_execution_id == execution_id:
                runtime.current_execution_id = None

        if shell_died:
            # One `exit` must not retire the terminal (the PTY path learned
            # this the hard way). The replacement is durably named before the
            # exit event goes out, so the user's next command lands in a live
            # shell rather than tripping over the corpse and burning its one
            # 404/409 retry.
            try:
                await self._replace_isolated_terminal_session(
                    session=session,
                    runtime=runtime,
                    runtime_identity=runtime_identity,
                    sandbox_id=sandbox_id,
                    agent_isolated_session_id=isolated_session_id,
                    old_terminal_session_id=target_session_id,
                    provider=provider,
                )
            except Exception as exc:
                logger.error(
                    "dead terminal shell replacement failed session=%s box=%s: %s",
                    session_id,
                    sandbox_id,
                    exc,
                )
        resolved_cwd = next_cwd or effective_cwd
        if runtime is not None:
            runtime.terminal_cwd = resolved_cwd or runtime.terminal_cwd
        if resolved_cwd:
            _terminal_cwd_cache[session_id] = resolved_cwd
            yield {"type": "__cwd__", "path": resolved_cwd}
        yield {
            "type": "exit",
            "exit_code": exit_code if exit_code is not None else provider_exit_code,
        }

    async def _replace_isolated_terminal_session(
        self,
        *,
        session: dict[str, Any],
        runtime: Any,
        runtime_identity: dict[str, Any],
        sandbox_id: str,
        agent_isolated_session_id: str,
        old_terminal_session_id: str | None,
        provider: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Replace one disposable terminal shell and durably name the new one."""

        old_id = str(old_terminal_session_id or "").strip()
        if old_id and old_id != agent_isolated_session_id:
            await provider.close_isolated_session(sandbox_id, old_id)

        try:
            uid = int(runtime_identity.get("uid") or 0)
            gid = int(runtime_identity.get("gid") or 0)
        except (TypeError, ValueError):
            uid = gid = 0
        home_dir = str(runtime_identity.get("home_dir") or "").rstrip("/")
        workspace_dir = str(runtime_identity.get("workspace_dir") or "").rstrip("/")
        workspace_source_dir = identity_workspace_source_dir(runtime_identity) or ""
        if (
            uid <= 0
            or gid <= 0
            or not home_dir
            or not workspace_dir
            or not workspace_source_dir
        ):
            raise APIError(
                code="CONVERSATION_IDENTITY_REQUIRED",
                message="shared terminal replacement requires uid, gid, home and workspace",
                status_code=409,
            )

        opened = await provider.open_isolated_session(
            sandbox_id,
            workspace_dir=workspace_dir,
            workspace_source_dir=workspace_source_dir,
            uid=uid,
            gid=gid,
            share_net=True,
            extra_writable=[home_dir],
        )
        new_id = str(opened.session_id or "").strip()
        updated_identity = {
            **runtime_identity,
            "terminal_isolated_session_id": new_id,
        }
        try:
            if self._sessions_repo is not None:
                await self._sessions_repo.update_session(
                    str(session.get("session_id") or ""),
                    {"runtime_identity": updated_identity},
                )
        except BaseException:
            with contextlib.suppress(Exception):
                await provider.close_isolated_session(sandbox_id, new_id)
            raise

        session["runtime_identity"] = updated_identity
        if runtime is not None:
            runtime.runtime_identity = updated_identity
            agent = getattr(runtime, "agent", None)
            inner = getattr(agent, "_inner", agent)
            for candidate in (agent, inner):
                binding = getattr(candidate, "isolated_binding", None)
                if binding is not None:
                    binding.terminal_isolated_session_id = new_id
                    break
        return new_id, updated_identity
