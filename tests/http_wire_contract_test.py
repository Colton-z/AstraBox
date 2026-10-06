"""Snapshot the complete HTTP surface produced by ``create_app()``.

The contract is a canonical OpenAPI document, stored as reviewable JSON. The
route table must also register deterministically and without method/path
collisions.

The fixture fixes environment-dependent inputs so local state cannot change the
snapshot: identity providers, feature flags, allowed hosts, tracing, env-file
loading, and the frontend distribution path. An empty frontend directory keeps
the API-only route shape deterministic.

The test constructs the app without entering its lifespan, so it does not touch
the network, database, or sandbox runtime.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

_DATA_DIR = Path(__file__).parent / "data"
_OPENAPI_SNAPSHOT_PATH = _DATA_DIR / "openapi_snapshot.json"


def _effective_routes(app: Any) -> list[tuple[tuple[str, ...], str, str]]:
    """Flatten every route ``create_app()`` registers to ``(methods, path, name)``.

    FastAPI 0.139 stores an ``include_router(...)`` target as a lazy
    ``_IncludedRouter`` holder on the parent's ``.routes`` rather than eagerly
    copying its children up — walking only ``app.routes`` would therefore miss
    every route mounted through ``app.include_router(_astrabox.router)``.
    Recursing into ``.original_router.routes`` resolves this; the
    child paths are already fully-qualified (``/api/v1/...``) because the
    router is constructed with no ``prefix=`` of its own and ``include_router``
    here is called with no ``prefix=`` either — verified by
    inspection, not assumed.
    """
    rows: list[tuple[tuple[str, ...], str, str]] = []

    def walk(routes: Any) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None) or getattr(route, "router", None)
            if inner is not None and getattr(inner, "routes", None) is not None:
                walk(inner.routes)
                continue
            path = getattr(route, "path", None)
            if not isinstance(path, str):
                continue
            methods = tuple(sorted(getattr(route, "methods", None) or ()))
            name = str(getattr(route, "name", None) or "")
            rows.append((methods, path, name))

    walk(app.routes)
    return rows


def _canonical_openapi_json(schema: dict[str, Any]) -> str:
    """Canonical (sorted-key, 2-space, real-unicode) serialization of a schema.

    Used identically to produce ``tests/data/openapi_snapshot.json`` and to
    re-serialize the live schema at test time, so the two are compared
    byte-for-byte rather than by some looser structural equality.
    """
    return json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


@contextmanager
def _deterministic_construction_env(frontend_dist: Path) -> Iterator[None]:
    """Monkeypatch exactly the env ``create_app()`` construction is sensitive
    to (see the module docstring), scoped to the ``with`` block so it never
    leaks into another test.
    """
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("ASTRABOX_ENV_FILE", str(frontend_dist.parent / "unused.env"))
        mp.setenv("ASTRABOX_WEB_IDENTITY", "local")
        mp.setenv("ASTRABOX_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
        mp.setenv("ASTRABOX_AGENT_ENABLED", "true")
        mp.setenv("ASTRABOX_FRONTEND_DIST", str(frontend_dist))
        # Build on the tracing-OFF path: init_tracing(app) reads this during
        # construction (instrument_app is middleware-only and cannot move the
        # route/openapi pin, but keep the build hermetic on any machine).
        mp.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        yield
    finally:
        mp.undo()


def _build_app(frontend_dist: Path) -> Any:
    """Construct the real app deterministically (no network; the app OBJECT
    only — the lifespan never runs)."""
    with _deterministic_construction_env(frontend_dist):
        from astrabox.api.app import create_app

        return create_app()


@pytest.fixture(scope="module")
def wire_app(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The app built once for this module's read-only route/schema pins."""
    frontend_dist = tmp_path_factory.mktemp("wire-contract") / "no-frontend-dist"
    return _build_app(frontend_dist)


# ── route table pin ──────────────────────────────────────────────────────────


def test_route_table_has_no_method_path_collisions(wire_app: Any) -> None:
    """No two DIFFERENT route objects claim the same (method, path).

    Starlette resolves routes in registration order and the first match wins,
    so a genuine collision would silently shadow one handler with another —
    this is a standing regression guard, independent of the frozen literal
    above, so a newly-introduced collision fails with a direct message rather
    than only showing up as an opaque list-diff.
    """
    seen: dict[tuple[str, str], str] = {}
    collisions: list[str] = []
    for methods, path, name in _effective_routes(wire_app):
        for method in methods:
            key = (method, path)
            if key in seen and seen[key] != name:
                collisions.append(f"{method} {path}: {seen[key]!r} vs {name!r}")
            else:
                seen[key] = name
    assert collisions == []


def test_route_registration_is_deterministic_across_constructions(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Two independent ``create_app()`` builds yield the identical route table.

    A standing guard for exactly the risk flagged while authoring this pin:
    each ``register_*_routes(app)`` module keys its idempotency guard off
    ``id(app)`` (a memory address), which CPython can reuse once the previous
    app object is garbage-collected — if that ever happens, a SECOND
    construction could silently register fewer routes than the first. This
    builds twice (fresh temp dirs, no shared state) and compares them to each
    other, so any nondeterminism — this one or a future one the E-b refactor
    might introduce — fails immediately instead of only showing up as
    unexplained flake in the frozen-snapshot test above.
    """
    dist_a = tmp_path_factory.mktemp("wire-contract-a") / "no-frontend-dist"
    dist_b = tmp_path_factory.mktemp("wire-contract-b") / "no-frontend-dist"
    first = sorted(_effective_routes(_build_app(dist_a)))
    second = sorted(_effective_routes(_build_app(dist_b)))
    assert first == second


# ── OpenAPI schema pin ───────────────────────────────────────────────────────


def test_openapi_schema_matches_committed_snapshot_file(wire_app: Any) -> None:
    canonical = _canonical_openapi_json(wire_app.openapi())
    on_disk = _OPENAPI_SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert canonical == on_disk
