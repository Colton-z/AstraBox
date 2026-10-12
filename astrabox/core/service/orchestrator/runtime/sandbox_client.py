"""Stateless helpers over a sandbox handle.

Unwrapping a composite handle, reading its id, and reaching the resident
server inside the box at its endpoint.
"""

import contextlib
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox import sandbox_for_sandbox

logger = get_logger(__name__)


def get_underlying_sandbox(sandbox_obj: Any) -> Any | None:
    """Extract the underlying sandbox object from a CodeInterpreter or return as-is."""
    if sandbox_obj is None:
        return None
    underlying = getattr(sandbox_obj, "sandbox", None)
    if underlying is not None and underlying is not sandbox_obj:
        return underlying
    return sandbox_obj


def extract_sandbox_id(sandbox: Any) -> str | None:
    for key in ("code_interpreter_id", "sandbox_id", "id"):
        value = getattr(sandbox, key, None)
        if value:
            return str(value)
    return None


async def resolve_enhanced_server_endpoint(
    sandbox_obj: Any,
    *,
    sandbox_id: str | None = None,
    port: int = 8000,
    connect_fn: Any = None,
) -> str | None:
    """Resolve the enhanced server endpoint for a sandbox.

    *connect_fn* is an async callable(sandbox_id) -> Sandbox used when the
    in-memory sandbox is unavailable and a transient connection is needed.
    """
    sandbox = get_underlying_sandbox(sandbox_obj)
    transient_sandbox = None

    if sandbox is None:
        effective_id = str(sandbox_id or "").strip()
        if not effective_id or connect_fn is None:
            return None
        try:
            transient_sandbox = await connect_fn(effective_id)
            sandbox = get_underlying_sandbox(transient_sandbox)
        except Exception as exc:
            logger.warning(
                "failed to resolve enhanced server endpoint: sandbox=%s err=%s",
                effective_id,
                exc,
            )
            return None

    if sandbox is None:
        return None

    try:
        return (await sandbox.get_endpoint(port)).url
    except Exception as exc:
        logger.warning(
            "failed to get enhanced server endpoint: sandbox=%s port=%s err=%s",
            sandbox_id or extract_sandbox_id(sandbox),
            port,
            exc,
        )
        return None
    finally:
        transient_underlying = get_underlying_sandbox(transient_sandbox)
        close = getattr(transient_underlying, "close", None) if transient_underlying else None
        if callable(close):
            with contextlib.suppress(Exception):
                await close()


async def check_server_health(sandbox: Any, port: int, timeout: float = 5.0) -> bool:
    try:
        endpoint = await sandbox.get_endpoint(port)
        dataplane = sandbox_for_sandbox(sandbox).build_dataplane(
            sandbox=sandbox, endpoint=endpoint.url, port=port
        )
        resp = await dataplane.request("GET", "/health", timeout=timeout)
        return resp.status_code == 200 and resp.text == "OK"
    except Exception:
        return False


async def reset_server_session(
    sandbox: Any,
    port: int,
    session_id: str,
    timeout: float = 5.0,
) -> None:
    endpoint = await sandbox.get_endpoint(port)
    dataplane = sandbox_for_sandbox(sandbox).build_dataplane(
        sandbox=sandbox, endpoint=endpoint.url, port=port
    )
    resp = await dataplane.request("GET", f"/reset/{session_id}", timeout=timeout)
    if resp.status_code != 200:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"failed to reset remote session {session_id}: {resp.status_code}",
            status_code=502,
        )
