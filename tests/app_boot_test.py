"""Unit smoke for the app surface — NO Docker, NO secret, NO live turn.

This is the non-e2e counterpart to ``tests/e2e/test_live_turn.py``: it runs in the
plain ``pytest`` (unit-CI) lane and proves the import-time/boot-time invariants the
app must hold, entirely in-process:

* ``create_app()`` is import-time constructible and returns a FastAPI app with the
  agent-platform routers mounted (the "routes mounted" gate, asserted
  by walking the *resolved* routes — FastAPI 0.138 stores included routers lazily,
  so ``_iter_route_paths`` recurses into ``_IncludedRouter.original_router``).
* the AI-SDK SSE serialization helpers emit the exact wire frames the frontend
  contract depends on:
  ``data: {json}\n\n``, the ``: keepalive\n\n`` comment, and the ``data: [DONE]\n\n``
  sentinel — the load-bearing host-side half of the Data-Stream-Protocol.

None of this touches the Docker daemon or the model endpoint, so it is green in CI
with no secret and gates every push/PR.
"""

from __future__ import annotations

import json


def _iter_route_paths(app) -> set[str]:
    """All route paths, recursing into FastAPI's lazy included-router holders."""
    paths: set[str] = set()

    def walk(routes) -> None:
        for r in routes:
            p = getattr(r, "path", None)
            if isinstance(p, str):
                paths.add(p)
            inner = getattr(r, "original_router", None) or getattr(r, "router", None)
            if inner is not None and getattr(inner, "routes", None) is not None:
                walk(inner.routes)

    walk(app.routes)
    return paths


def test_create_app_constructs_and_mounts_routes() -> None:
    from astrabox.api.app import create_app

    app = create_app()
    assert app.__class__.__name__ == "FastAPI"

    paths = _iter_route_paths(app)
    # Liveness and the core turn routes must be mounted together so the frontend
    # cannot boot against an API missing its conversation surface.
    assert "/healthz" in paths
    assert "/api/v1/sessions/{session_id}" in paths
    assert "/api/v1/sessions/{session_id}/ai-stream" in paths
    # Sessions are created only as Agent or Assistant conversations
    # (docs/domain-model.md §2).
    assert "/api/v1/agents" in paths
    assert "/api/v1/agents/{agent_id}/extension-console/session" in paths
    assert "/extension-console/open" in paths
    assert "/ui/{ui_path:path}" in paths


def test_data_stream_sse_frame_helpers() -> None:
    from astrabox.api.sse import (
        format_sse_done,
        format_sse_event,
        format_sse_keepalive,
    )

    # A part frame serializes as a single `data: {json}\n\n` SSE event.
    frame = format_sse_event({"type": "text-delta", "id": "blk-0", "delta": "hi"})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame[len("data: "):].strip())
    assert payload == {"type": "text-delta", "id": "blk-0", "delta": "hi"}

    # The terminal sentinel + the idle keepalive comment.
    assert format_sse_done() == "data: [DONE]\n\n"
    assert format_sse_keepalive().startswith(":")
    assert format_sse_keepalive().endswith("\n\n")
