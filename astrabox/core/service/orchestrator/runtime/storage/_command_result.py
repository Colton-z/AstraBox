"""Command-result extraction helpers shared across the storage package.

Tiny synchronous helpers that pull stdout/stderr/log text out of a sandbox
command-run result and raise ``APIError`` when a run failed.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.diagnostics import extract_command_log_text


# ── Storage command helpers ───────────────────────────────────────


def _extract_command_log_text(result: Any) -> str:
    return extract_command_log_text(result)


def _extract_command_output_text(result: Any) -> str:
    chunks: list[str] = []
    for attr in ("stdout", "output"):
        value = getattr(result, attr, None)
        if value:
            chunks.append(str(value))
    log_text = _extract_command_log_text(result)
    if log_text:
        chunks.append(log_text)
    return "".join(chunks)


def _ensure_command_success(result: Any, code: str, message: str) -> None:
    error = getattr(result, "error", None)
    exit_code = getattr(result, "exit_code", None)
    if error is None and (exit_code is None or exit_code == 0):
        return
    output = _extract_command_output_text(result).strip()
    raise APIError(
        code=code,
        message=f"{message}: {error or f'exit_code={exit_code}'}; output={output[:2000]!r}",
        status_code=502,
    )


def _extract_command_stream_text(result: Any, stream_name: str) -> str:
    logs = getattr(result, "logs", None)
    if logs is None:
        return ""

    stream = getattr(logs, stream_name, None)
    if not stream:
        return ""

    chunks: list[str] = []
    for item in stream:
        text = getattr(item, "text", None)
        chunks.append(str(text if text is not None else item))
    return "".join(chunks)
