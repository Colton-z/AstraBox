"""Where a terminal command runs: the session's shell in its box.

:meth:`TerminalBox.prepare` resolves a session's box and shell and refuses a
session whose terminal cannot run (``409``). :meth:`TerminalBox.run` runs one
command there and yields ``stdout`` and ``stderr`` events as the shell writes
them, then a ``cwd`` event naming the directory the shell ended in and an
``exit`` event with the command's status. The status comes from a marker the
platform prints after the command, because the shell process outlives the
command and its own exit describes the shell.

A session's shell is one of two kinds:

* a conversation with a box of its own uses one long-lived bash process in
  the box (:class:`~astrabox.core.service.orchestrator.runtime.pty_terminal.PtyTerminal`),
  named by its process id, which any replica can attach to;
* a conversation in a shared box uses a terminal-only OpenSandbox isolation
  session beside the Agent's, with the same uid and workspace, named by
  ``runtime_identity["terminal_isolated_session_id"]``.

Cancelling the task that consumes :meth:`TerminalBox.run` interrupts the
command in the box: the bash process receives ``SIGINT``; an isolation session
is replaced, because closing its stream does not stop what runs in it.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    identity_workspace_dir,
    identity_workspace_source_dir,
    normalize_runtime_identity,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import PtyTerminal
from astrabox.core.service.orchestrator.runtime.sandbox_client import get_underlying_sandbox
from astrabox.core.service.orchestrator.runtime_binding import (
    is_assistant_user_conversation,
    reconcile_session_runtime_binding,
)
from astrabox.seams.sandbox import sandbox_for_name

logger = get_logger(__name__)


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


@dataclass(slots=True)
class TerminalTarget:
    """A session's terminal, resolved for one command.

    ``default_cwd`` is where a command starts when neither the request nor an
    earlier command names a directory. ``sandbox`` is the box connection a
    box-level shell is reached through; an isolation-session terminal is
    reached through the provider by id and has none.
    """

    session: dict[str, Any]
    session_id: str
    sandbox_id: str | None
    runtime_identity: dict[str, Any] | None
    isolated_session_id: str
    terminal_isolated_session_id: str
    default_cwd: str | None
    sandbox: Any = None

    async def close(self) -> None:
        """Release the box connection, if this target holds one."""
        sandbox, self.sandbox = self.sandbox, None
        close = getattr(sandbox, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()


ShellBound = Callable[[str | None], Awaitable[None]]


class TerminalBox:
    def __init__(
        self,
        *,
        runtime_manager: Any,
        sessions_repo: Any,
        assistant_workspace_service: Any | None = None,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._sessions_repo = sessions_repo
        self._assistant_workspace_service = assistant_workspace_service

    async def prepare(self, session: dict[str, Any]) -> TerminalTarget:
        """Resolve where the session's terminal runs; ``409`` when it cannot run now."""
        from astrabox.core.service.orchestrator.runtime.environment_ownership import require_environment_ownership

        require_environment_ownership(session)
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
        session_id = str(session.get("session_id") or "")
        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        session_kind = require_session_kind(session.get("session_kind"))
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))
        if sandbox_id:
            from astrabox.common.utils.settings import load_astrabox_settings
            from astrabox.core.service.orchestrator.engine.provisioning import require_live_credential_delivery

            require_live_credential_delivery(
                runtime_identity=runtime_identity, sandbox_id=sandbox_id,
                vault_enabled=bool(load_astrabox_settings().sandbox_credential_vault_enabled),
            )
        if session_kind == "agent_chat" and runtime_identity is None:
            raise APIError(
                code="CONVERSATION_IDENTITY_REQUIRED",
                message="agent_chat terminal access requires runtime_identity",
                status_code=409,
            )
        isolated_session_id = str((runtime_identity or {}).get("isolated_session_id") or "").strip()
        default_cwd = identity_workspace_dir(runtime_identity) or self._runtime_manager.resolve_session_terminal_cwd(
            session_id,
            sandbox_id=sandbox_id,
            session_kind=session_kind,
            engine_session_key=str(session.get("engine_session_key") or "").strip() or None,
        )
        sandbox = None
        if not isolated_session_id:
            if not sandbox_id:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox not available for terminal access",
                    status_code=409,
                )
            sandbox = await self._runtime_manager.connect_sandbox_only(sandbox_id)
        return TerminalTarget(
            session=session,
            session_id=session_id,
            sandbox_id=sandbox_id,
            runtime_identity=runtime_identity,
            isolated_session_id=isolated_session_id,
            terminal_isolated_session_id=str(
                (runtime_identity or {}).get("terminal_isolated_session_id") or ""
            ).strip(),
            default_cwd=default_cwd,
            sandbox=sandbox,
        )

    async def run(
        self,
        target: TerminalTarget,
        command: str,
        *,
        cwd: str | None,
        shell_id: str | None,
        on_shell: ShellBound,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run ``command`` in the session's shell and yield its events.

        ``shell_id`` is the box process the session's shell last ran as. A
        shell that is gone is replaced, and ``on_shell`` receives the new one's
        id; it receives ``None`` when the shell failed and must not be reused.
        """
        credential_env = await self._credential_env(
            target.session_id,
            runtime_identity=target.runtime_identity,
        )
        if target.isolated_session_id:
            async for event in self._run_isolated(target, command, cwd=cwd, credential_env=credential_env):
                yield event
            return
        async for event in self._run_shell(
            target, command, cwd=cwd, shell_id=shell_id, on_shell=on_shell, credential_env=credential_env
        ):
            yield event

    async def _credential_env(
        self,
        session_id: str,
        *,
        runtime_identity: dict[str, Any] | None,
    ) -> dict[str, str]:
        """Opaque Vault placeholders safe to expose to this Session's process.

        They are minted under the context the Session's runtime recorded, so a
        terminal command and the engine send the placeholders the backend
        binds. A Session with environment credentials whose runtime records no
        context is refused rather than given placeholders that match nothing.
        """
        from astrabox.core.service.orchestrator.engine.provisioning import (
            ENVIRONMENT_PLACEHOLDER_CONTEXT,
        )

        context = str((runtime_identity or {}).get(ENVIRONMENT_PLACEHOLDER_CONTEXT) or "").strip()
        credentials = await self._runtime_manager.resolve_session_egress_credentials(
            session_id,
            **({"placeholder_context": context} if context else {}),
        )
        environment = {
            str(credential.secret_name): str(credential.placeholder)
            for credential in credentials
            if str(getattr(credential, "secret_name", "") or "").strip()
            and str(getattr(credential, "placeholder", "") or "").strip()
        }
        if environment and not context:
            raise APIError(
                code="SANDBOX_CREDENTIAL_CONTEXT_UNAVAILABLE",
                message=(
                    f"Session {session_id!r} uses environment credentials, but its "
                    "runtime records no environment placeholder context; the "
                    "conversation needs a new sandbox."
                ),
                status_code=409,
            )
        return environment

    async def _run_shell(
        self,
        target: TerminalTarget,
        command: str,
        *,
        cwd: str | None,
        shell_id: str | None,
        on_shell: ShellBound,
        credential_env: dict[str, str],
    ) -> AsyncIterator[dict[str, Any]]:
        """Run in the session's long-lived bash process in its box."""
        try:
            terminal = PtyTerminal(get_underlying_sandbox(target.sandbox).processes)
            known = str(shell_id or "").strip()
            if known and await terminal.session_exists(known):
                pty_session_id = known
            else:
                # A new shell starts in the command's directory; a live one
                # stays where its last command left it.
                pty_session_id = await terminal.open_session(cwd=cwd, envs=credential_env or None)
                await on_shell(pty_session_id)
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
        except asyncio.CancelledError as cancelled:
            # Stopping the read leaves the command running in the shell; the
            # interrupt is the shell process's own SIGINT, sent even while the
            # task that asked for it is being cancelled.
            interrupt = asyncio.create_task(terminal.interrupt(pty_session_id))
            try:
                await asyncio.shield(interrupt)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await interrupt
            except Exception as exc:
                logger.error(
                    "terminal interrupt failed session=%s shell=%s: %s",
                    target.session_id,
                    pty_session_id,
                    exc,
                )
            raise cancelled
        except Exception as exc:
            # The shell may have gone with its box. Forget it, so the next
            # command opens a new one rather than retrying a corpse.
            await on_shell(None)
            yield {"type": "stderr", "text": f"Command execution error: {exc}\n"}
            yield {"type": "exit", "exit_code": 1}
            return

        resolved_cwd = next_cwd or cwd
        if resolved_cwd:
            yield {"type": "cwd", "path": resolved_cwd}
        yield {"type": "exit", "exit_code": exit_code}

    async def _run_isolated(
        self,
        target: TerminalTarget,
        command: str,
        *,
        cwd: str | None,
        credential_env: dict[str, str],
    ) -> AsyncIterator[dict[str, Any]]:
        """Run through a terminal-only OpenSandbox isolation session.

        The Agent runner lives in ``isolated_session_id``. The terminal lives
        in a sibling session with the same uid and workspace, so deleting a
        stuck terminal cannot kill or serialize against the Agent runner.
        """

        session = target.session
        session_id = target.session_id
        sandbox_id = target.sandbox_id
        runtime_identity = target.runtime_identity
        isolated_session_id = target.isolated_session_id
        if not sandbox_id or runtime_identity is None:
            yield {
                "type": "stderr",
                "text": "Command execution error: isolated terminal placement is incomplete\n",
            }
            yield {"type": "exit", "exit_code": 1}
            return
        backend = str(session.get("sandbox_backend") or "").strip().lower()
        if not backend:
            yield {
                "type": "stderr",
                "text": "Command execution error: sandbox backend is missing\n",
            }
            yield {"type": "exit", "exit_code": 1}
            return

        provider = sandbox_for_name(backend)
        marker = f"__ASTRABOX_ISOLATED_TERMINAL_{uuid.uuid4().hex}__"
        marker_re = re.compile(rf"{re.escape(marker)}:(-?\d+):([^\r\n]*)")
        code = _isolated_terminal_code(
            command,
            cwd=cwd,
            identity=runtime_identity,
            marker=marker,
        )
        exit_code: int | None = None
        next_cwd: str | None = None
        provider_exit_code = 1
        shell_died = False
        target_session_id = target.terminal_isolated_session_id
        try:
            if not target_session_id or target_session_id == isolated_session_id:
                target_session_id, runtime_identity = (
                    await self._replace_isolated_terminal_session(
                        session=session,
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

        if shell_died:
            # One `exit` must not retire the terminal (the PTY path learned
            # this the hard way). The replacement is durably named before the
            # exit event goes out, so the user's next command lands in a live
            # shell rather than tripping over the corpse and burning its one
            # 404/409 retry.
            try:
                await self._replace_isolated_terminal_session(
                    session=session,
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
        resolved_cwd = next_cwd or cwd
        if resolved_cwd:
            yield {"type": "cwd", "path": resolved_cwd}
        yield {
            "type": "exit",
            "exit_code": exit_code if exit_code is not None else provider_exit_code,
        }

    async def _replace_isolated_terminal_session(
        self,
        *,
        session: dict[str, Any],
        runtime_identity: dict[str, Any],
        sandbox_id: str,
        agent_isolated_session_id: str,
        old_terminal_session_id: str | None,
        provider: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Replace one disposable terminal shell and durably name the new one.

        The conversation's runtime in this process, if there is one, learns the
        new name too: releasing the conversation closes the terminal session
        its binding names.
        """

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
        session_id = str(session.get("session_id") or "")
        try:
            # A terminal shell is not conversation activity, so the session
            # keeps its place in its owner's list.
            await self._sessions_repo.update_session(
                session_id,
                {"runtime_identity": updated_identity},
                touch_updated_at=False,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                await provider.close_isolated_session(sandbox_id, new_id)
            raise

        session["runtime_identity"] = updated_identity
        runtime = self._runtime_manager.get_runtime(session_id, sandbox_id=sandbox_id)
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
