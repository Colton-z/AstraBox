"""Verify that literal frontend API paths match the backend's registered routes.

Frontend sources are checked for literal ``/api/v1`` paths. Clients that build
paths from a configured base are excluded because they contain no complete
literal to extract.

The registered route set comes from a built app and includes nested FastAPI
routers. Agent routes are enabled because the console always exposes that
surface.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"

# Scan every frontend source by default. These clients build paths from a
# configured base and contain no complete `/api/v1` literal to extract.
_UNSCANNABLE_CLIENTS = frozenset(
    {
        _FRONTEND_SRC / "assistant" / "api.ts",
        _FRONTEND_SRC / "admin" / "AdminApp.tsx",
    }
)


def _literal_path_sources() -> list[Path]:
    return sorted(
        path
        for path in _FRONTEND_SRC.rglob("*")
        if path.suffix in {".ts", ".tsx"}
        and not path.name.endswith((".test.ts", ".test.tsx", ".d.ts"))
        and path not in _UNSCANNABLE_CLIENTS
    )


def _strip_comments(source: str) -> str:
    """Remove `//` and `/* */` comments, preserving line structure.

    Documentation may name unsupported routes without calling them. Excluding
    comments keeps this check focused on executable client paths.
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<![:'\"`\\])//[^\n]*", "", without_block)

# Where a path literal in the client ends. `)` and `,` close a `request(...)`
# call whose path was not quoted to the end; the quotes close the literal.
_PATH_TERMINATORS = set("'\"`) ,\n\t")


def _iter_route_paths(app) -> set[str]:
    """Every registered route path, including lazily-included routers.

    Mirrors ``tests/app_boot_test.py``: FastAPI keeps an included router in an
    ``_IncludedRouter`` holder whose own ``path`` is ``None``, so a flat pass
    over ``app.routes`` silently misses every ``include_router`` route.
    """
    paths: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
            inner = getattr(route, "original_router", None) or getattr(route, "router", None)
            if inner is not None and getattr(inner, "routes", None) is not None:
                walk(inner.routes)

    walk(app.routes)
    return paths


def _scan_client_paths(source: str) -> set[str]:
    """The ``/api/v1/...`` paths ``api.ts`` builds, normalized for comparison.

    A path in the client is a template literal: ``/api/v1/sessions/${id}/share``.
    Scanning is brace-aware because an interpolation can nest its own template
    literal (``${qs ? `?${qs}` : ''}``), which a non-greedy regex would cut in
    the wrong place.

    Two normalizations make the two sides comparable:

    * An interpolation *preceded by a slash* is a path parameter and becomes
      ``{}`` — the same placeholder every ``{session_id}`` on the server side
      collapses to. An interpolation *glued to a literal segment*
      (``/admin/sandboxes${suffix}``) is a query-string suffix, not a segment,
      so the path ends there.
    * A bare ``{session_id}`` preceded by a slash is the same parameter written
      in the server's own template form, which is what the generated client
      takes: ``client.GET('/api/v1/sessions/{session_id}', ...)``. It collapses
      to ``{}`` alongside the interpolated form, so both spellings are compared
      against the route table the same way. The parameter's NAME is checked
      where it is knowable — the generated `paths` type refuses a path, or a
      parameter name, the document does not carry, so a wrong one fails to
      compile rather than reaching here.
    * A literal ``?`` ends the path for the same reason.
    """
    found: set[str] = set()
    for match in re.finditer(r"/api/v1", source):
        i = match.start()
        out: list[str] = []
        depth = 0
        while i < len(source):
            ch = source[i]
            if depth == 0 and ch in _PATH_TERMINATORS:
                break
            if depth == 0 and ch == "?":
                break
            if (ch == "$" and source[i + 1 : i + 2] == "{") or ch == "{":
                if not out or out[-1] != "/":
                    # Glued to a literal segment → a query suffix, not a segment.
                    break
                depth = 1
                i += 2 if ch == "$" else 1
                while i < len(source) and depth > 0:
                    if source[i] == "{":
                        depth += 1
                    elif source[i] == "}":
                        depth -= 1
                    i += 1
                out.append("{}")
                depth = 0
                continue
            out.append(ch)
            i += 1
        path = "".join(out).rstrip("/")
        if path.startswith("/api/v1"):
            found.add(path)
    return found


def _normalize_route(path: str) -> str:
    """``/api/v1/agents/{agent_id}`` → ``/api/v1/agents/{}``."""
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/")


def _enable_conditional_auth_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The /api/v1/auth/* routes register only under the oidc identity mode
    (a door to nowhere in the other modes) — same situation as the agent flag
    above: the console always ships the callers, so the census builds the app
    with the mode on, satisfied by dummy config (read at registration, never
    dialed during enumeration)."""
    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", "oidc")
    monkeypatch.setenv("ASTRABOX_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_ID", "route-census")
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", "route-census-secret-0123456789abcdef")


@pytest.fixture
def registered_paths(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    monkeypatch.setenv("ASTRABOX_AGENT_ENABLED", "1")
    _enable_conditional_auth_routes(monkeypatch)
    from astrabox.api.app import create_app

    return {_normalize_route(p) for p in _iter_route_paths(create_app())}


def test_the_console_client_only_calls_routes_the_backend_serves(
    registered_paths: set[str],
) -> None:
    """Every literal console path must have a registered backend route."""
    missing: list[str] = []
    scanned_any = False
    for source in _literal_path_sources():
        called = _scan_client_paths(_strip_comments(source.read_text(encoding="utf-8")))
        if not called:
            continue
        scanned_any = True
        rel = source.relative_to(_REPO_ROOT)
        missing += [f"{rel}: {p}" for p in sorted(called) if p not in registered_paths]

    # A successful scan must find at least one route-bearing source file.
    assert scanned_any, "found no /api/v1 paths anywhere — the scanner stopped working"

    assert not missing, (
        "the console names paths this backend does not serve — every one of "
        f"these is an HTTP 404 the moment its page opens: {missing}"
    )


def test_the_scan_reads_the_shapes_the_client_actually_writes() -> None:
    """The scanner recognizes every path-literal shape used by the client."""
    scanned = _scan_client_paths(
        """
        request('/api/v1/user/current');
        request(`/api/v1/sessions/${sessionId}/share`);
        request(`/api/v1/admin/sessions/all?limit=${limit}`);
        request(`/api/v1/admin/sandboxes${suffix ? `?${suffix}` : ''}`);
        request(`/api/v1/agents/${a}/deployments/${w}`);
        """
    )
    assert scanned == {
        "/api/v1/user/current",
        "/api/v1/sessions/{}/share",
        "/api/v1/admin/sessions/all",
        "/api/v1/admin/sandboxes",
        "/api/v1/agents/{}/deployments/{}",
    }


def test_a_retired_path_is_caught(registered_paths: set[str]) -> None:
    """An unsupported console path must remain outside the registered route set."""
    retired = _scan_client_paths("request('/api/v1/admin/templates');")
    assert retired == {"/api/v1/admin/templates"}
    assert not retired & registered_paths
