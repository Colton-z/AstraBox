"""Runtime start diagnostics.

Collects sandbox log hints when runtime initialization fails.
"""

import asyncio
import contextlib
import shlex
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    normalize_runtime_identity,
)

logger = get_logger(__name__)

_LIFECYCLE_DIAGNOSTICS_TIMEOUT_SECONDS = 10.0
_LIFECYCLE_DIAGNOSTICS_MAX_CHARS = 2400
_LIFECYCLE_DIAGNOSTIC_SCOPES = ("inspect", "events", "logs")
_LIFECYCLE_DIAGNOSTIC_SECTION_MAX_CHARS = 760


def is_initialize_timeout_error(exc: BaseException) -> bool:
    if "control request timeout: initialize" in str(exc).lower():
        return True
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)):
        return any(
            is_initialize_timeout_error(item)
            for item in nested
            if isinstance(item, BaseException)
        )
    return False


def is_claude_server_start_timeout_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "claude code server failed to start within timeout period" in text:
        return True
    if "enhanced ws server failed to start within timeout" in text:
        return True
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)):
        return any(
            is_claude_server_start_timeout_error(item)
            for item in nested
            if isinstance(item, BaseException)
        )
    return False


def build_runtime_start_error_message(
    exc: BaseException,
    diagnostic_detail: str | None,
) -> str:
    base = f"failed to start remote-agent runtime: {exc}"
    detail = str(diagnostic_detail or "").strip()
    if not detail:
        return base
    return f"{base}; {detail}"


def normalize_diag_text(raw: str) -> str:
    lines = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return " ; ".join(lines)


def extract_command_log_text(result: Any) -> str:
    logs = getattr(result, "logs", None)
    if logs is None:
        return ""

    chunks: list[str] = []
    for stream_name in ("stdout", "stderr"):
        stream = getattr(logs, stream_name, None)
        if not stream:
            continue
        for item in stream:
            text = getattr(item, "text", None)
            if text is None:
                text = str(item)
            chunks.append(str(text))
    return "".join(chunks)


def _compact_lifecycle_diagnostic_section(raw: str, *, keep_tail: bool) -> str:
    text = normalize_diag_text(raw)
    if len(text) <= _LIFECYCLE_DIAGNOSTIC_SECTION_MAX_CHARS:
        return text
    kept = _LIFECYCLE_DIAGNOSTIC_SECTION_MAX_CHARS - 4
    return f"... {text[-kept:]}" if keep_tail else f"{text[:kept]} ..."


async def read_lifecycle_diagnostics(
    provider: Any,
    sandbox_id: str,
) -> str:
    """Read diagnostics without depending on the sandbox's exec channel."""
    read_fn = getattr(provider, "read_diagnostics", None)
    if not sandbox_id or not callable(read_fn):
        return ""

    async def read_scope(scope: str) -> str:
        try:
            report = await asyncio.wait_for(
                read_fn(sandbox_id, scope=scope),
                timeout=_LIFECYCLE_DIAGNOSTICS_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "sandbox lifecycle diagnostics unavailable: sandbox=%s "
                "provider=%s scope=%s error=%s",
                sandbox_id,
                str(getattr(provider, "name", "unknown") or "unknown"),
                scope,
                str(getattr(exc, "code", "") or type(exc).__name__),
            )
            return ""
        return _compact_lifecycle_diagnostic_section(
            str(getattr(report, "text", "") or ""),
            keep_tail=scope in {"events", "logs"},
        )

    reports = await asyncio.gather(
        *(read_scope(scope) for scope in _LIFECYCLE_DIAGNOSTIC_SCOPES)
    )
    parts = [
        f"{scope}: {report}"
        for scope, report in zip(_LIFECYCLE_DIAGNOSTIC_SCOPES, reports, strict=True)
        if report
    ]
    return " | ".join(parts)[:_LIFECYCLE_DIAGNOSTICS_MAX_CHARS]


async def run_sandbox_diag_command(
    sandbox: Any,
    command: str,
    timeout_seconds: float = 6.0,
) -> str:
    command_runner = getattr(sandbox, "commands", None)
    if command_runner is None:
        return ""

    run_fn = getattr(command_runner, "run", None)
    if not callable(run_fn):
        return ""

    try:
        result = await asyncio.wait_for(run_fn(command), timeout=timeout_seconds)
    except Exception:
        return ""

    return extract_command_log_text(result)


async def collect_runtime_start_diagnostics(
    sandbox_handle: Any,
    *,
    session_id: str,
    exc: BaseException,
    sandbox_provider: Any,
    get_underlying_sandbox_fn: Any,
    extract_sandbox_id_fn: Any,
    runtime_identity: dict[str, Any] | None = None,
) -> str | None:
    initialize_timeout = is_initialize_timeout_error(exc)
    sidecar_start_timeout = is_claude_server_start_timeout_error(exc)

    sandbox = get_underlying_sandbox_fn(sandbox_handle)
    sandbox_id = extract_sandbox_id_fn(sandbox_handle) or extract_sandbox_id_fn(sandbox)
    if sandbox is None:
        if sidecar_start_timeout:
            return "sidecar start timeout; sandbox diagnostics unavailable"
        if initialize_timeout:
            return "mcp initialize timeout; sandbox diagnostics unavailable"
        return "runtime start failure; sandbox diagnostics unavailable"

    # This report comes from the provider's lifecycle control plane, not from
    # execd inside the box. Read it before the command-based hints so a dead
    # exec channel cannot erase the Pod/container evidence immediately before
    # startup cleanup destroys the sandbox.
    lifecycle_detail = await read_lifecycle_diagnostics(sandbox_provider, sandbox_id)
    identity = normalize_runtime_identity(runtime_identity)
    runner_logs = "/tmp/astrabox_runner.log"
    if identity:
        runner_logs += " " + shlex.quote(f"{identity['home_dir']}/.astrabox-runner.log")

    # Generation logs, process table and supervisor state come first: the
    # claude.log httpx connect noise is bulky and the detail gets truncated,
    # which would otherwise hide the actual generation bring-up failure.
    generic_hint = await run_sandbox_diag_command(
        sandbox,
        "echo '--- generations dir ---'; "
        "ls -la /tmp/.claude/generations/ 2>&1 | tail -n 6; "
        "for f in "
        "/tmp/.claude/generations/*.sidecar.log "
        "/tmp/.claude/generations/*.model_proxy.log; do "
        "if [ -f \"$f\" ]; then "
        "echo \"--- $f ---\"; "
        "tail -c 1500 \"$f\" 2>/dev/null || true; "
        "fi; "
        "done; "
        "echo '--- processes ---'; "
        "ps -ef | grep -E 'sandbox_runner|claude' | grep -v grep || true; "
        # The runner's own log. `runner link closed during preparation` is the
        # host's view of a 1011 the runner sent when its activate handler
        # raised, and the traceback is in THIS file and nowhere else: the Pod
        # logs above are supervisord and execd. Every such failure so far was
        # reported without its cause because this was never read.
        f"for f in {runner_logs}; do "
        "if [ -f \"$f\" ]; then "
        "echo \"--- $f ---\"; "
        "tail -n 60 \"$f\" 2>/dev/null || true; "
        "fi; "
        "done; "
        "for f in "
        "/root/claude.log /tmp/claude.log ./claude.log; do "
        "if [ -f \"$f\" ]; then "
        "echo \"--- $f ---\"; "
        "grep -iE 'error|failed|failure|exception|traceback|timeout|initialize|unauthorized|forbidden|status=|status |received 1000|select language' \"$f\" 2>/dev/null | grep -v connect_tcp | tail -n 40 || true; "
        "fi; "
        "done",
        timeout_seconds=8.0,
    )
    generic_detail = normalize_diag_text(generic_hint)

    if not sidecar_start_timeout and not initialize_timeout:
        details = [
            value
            for value in (
                f"lifecycle: {lifecycle_detail}" if lifecycle_detail else "",
                f"in-box: {generic_detail}" if generic_detail else "",
            )
            if value
        ]
        if not details:
            return "runtime start failure; no sandbox log hints captured"
        detail = " | ".join(details)[:3500]
        logger.warning(
            "runtime start diagnostics: session=%s sandbox=%s detail=%s",
            session_id,
            sandbox_id,
            detail,
        )
        return f"runtime start failure diagnostics: {detail}"

    if sidecar_start_timeout and not initialize_timeout:
        details = [
            value
            for value in (
                f"lifecycle: {lifecycle_detail}" if lifecycle_detail else "",
                f"in-box: {generic_detail}" if generic_detail else "",
            )
            if value
        ]
        if not details:
            return "sidecar start timeout; no sandbox log hints captured"
        detail = " | ".join(details)[:3500]
        logger.warning(
            "runtime start diagnostics: session=%s sandbox=%s detail=%s",
            session_id,
            sandbox_id,
            detail,
        )
        return f"sidecar start timeout diagnostics: {detail}"

    claude_hint = await run_sandbox_diag_command(
        sandbox,
        "grep -iE 'mcp|error|timeout|initialize|failed' /root/claude.log 2>/dev/null | tail -n 8",
    )
    debug_root = str((identity or {}).get("config_dir") or "/root/.claude").rstrip("/")
    debug_hint = await run_sandbox_diag_command(
        sandbox,
        f"f=$(ls -1t {shlex.quote(debug_root)}/debug/*.txt 2>/dev/null | head -n 1); "
        "if [ -n \"$f\" ]; then "
        "grep -iE 'mcp|error|timeout|initialize|failed' \"$f\" 2>/dev/null | tail -n 8; "
        "fi",
    )

    hints: list[str] = []
    if lifecycle_detail:
        hints.append(f"lifecycle: {lifecycle_detail}")
    if generic_detail:
        hints.append(f"in-box: {generic_detail}")
    for text in (claude_hint, debug_hint):
        normalized = normalize_diag_text(text)
        if normalized:
            hints.append(normalized)

    if not hints:
        return "mcp initialize timeout; no sandbox log hints captured"

    detail = " | ".join(hints)
    detail = detail[:900]
    logger.warning(
        "runtime start diagnostics: session=%s sandbox=%s detail=%s",
        session_id,
        sandbox_id,
        detail,
    )
    return f"mcp initialize timeout diagnostics: {detail}"
