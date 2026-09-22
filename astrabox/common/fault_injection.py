"""Neutral fault-injection hook points for deterministic failure testing.

Production code calls :func:`consume_fault` at a named hook point and gets
``False`` unless a handler was explicitly installed — no file IO, no env reads,
no test logic on the hot path. The only included installer is the E2E harness
(:mod:`astrabox.testing.e2e_faults`), armed at app startup solely when
``ASTRABOX_E2E_FAULTS`` is set.

This module exists so the core never imports test support code: the dependency
arrow is testing → common, and with nothing installed the seam is structurally
inert rather than inert-by-flag-check.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable
from typing import Any, Callable

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

FaultHook = Callable[..., bool]
FaultBarrierHook = Callable[..., Awaitable[None]]

_hooks: dict[str, FaultHook] = {}
_barrier_hooks: dict[str, FaultBarrierHook] = {}
_lock = threading.Lock()


def register_fault_hook(name: str, hook: FaultHook) -> None:
    """Install *hook* for the named hook point (last registration wins)."""
    with _lock:
        _hooks[name] = hook


def register_fault_barrier(name: str, hook: FaultBarrierHook) -> None:
    """Install an async barrier at a named hook point (last registration wins)."""
    with _lock:
        _barrier_hooks[name] = hook


def clear_fault_hooks() -> None:
    """Remove every installed hook (test teardown)."""
    with _lock:
        _hooks.clear()
        _barrier_hooks.clear()


def consume_fault(name: str, **context: Any) -> bool:
    """Return True when an armed fault at hook point *name* fires for *context*.

    Never raises: a broken fault hook must not take down the production path
    it instruments — it is logged and treated as "no fault".
    """
    hook = _hooks.get(name)
    if hook is None:
        return False
    try:
        return bool(hook(**context))
    except Exception as exc:  # noqa: BLE001 - the seam must never break its host
        logger.warning("fault hook %s failed (ignored): %s", name, exc)
        return False


async def pass_fault_barrier(name: str, **context: Any) -> None:
    """Wait at an installed test barrier; return immediately in production."""
    hook = _barrier_hooks.get(name)
    if hook is None:
        return
    await hook(**context)
