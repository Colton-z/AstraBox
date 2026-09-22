"""Single-source-of-truth resolver for a session's engine_kind.

engine_kind may live in any of three places:
  - ``runtime.engine_kind`` (set when the in-memory runtime exists)
  - ``session["engine_kind"]`` (hydrated onto the durable session row)
  - ``session["workspace_ref"]["engine_kind"]`` (the workspace binding)

This applies the same precedence as ``LifecycleWorker._session_engine_kind``:
runtime first (most current), then the durable session row, then workspace_ref.
Creation chooses a product default before it writes the Session; a read never
repairs a missing identity by guessing another engine. Call this directly
rather than re-implementing the chain.
"""

from __future__ import annotations

from typing import Any


def resolve_session_engine_kind(
    session: dict[str, Any] | None,
    *,
    runtime: Any | None = None,
) -> str:
    """Return the engine_kind for a session.

    Walks ``runtime → session → workspace_ref`` and returns the first
    non-empty value. ``session_kind`` is a platform product and is validated
    independently; it is not an engine identity fallback. ``runtime`` is
    keyword-only so the common case reads as
    ``resolve_session_engine_kind(session)`` without a stray ``None``.
    """
    from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind

    require_session_kind((session or {}).get("session_kind"))
    workspace_ref = (session or {}).get("workspace_ref")
    runtime_engine = str(getattr(runtime, "engine_kind", "") or "").strip()
    session_engine = str((session or {}).get("engine_kind") or "").strip()
    workspace_engine = (
        str(workspace_ref.get("engine_kind") or "").strip()
        if isinstance(workspace_ref, dict)
        else ""
    )
    declared = {
        source: value
        for source, value in (
            ("runtime", runtime_engine),
            ("session", session_engine),
            ("workspace_ref", workspace_engine),
        )
        if value
    }
    if len(set(declared.values())) > 1:
        raise ValueError(f"session engine_kind declarations disagree: {declared!r}")
    engine_kind = runtime_engine or session_engine or workspace_engine
    if not engine_kind:
        raise ValueError("session engine_kind is required")
    return engine_kind
