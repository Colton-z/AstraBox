"""Every route reaching an administrator-only service stays under the admin path.

The platform administrator check exists in exactly one place: the
``/api/v1/admin`` prefix test in
:mod:`astrabox.web.identity_middleware`. The services behind that prefix —
``AdminService``, ``VaultService``, ``MCPRegistryService``,
``CredentialBindingService`` — hold no ``is_platform_admin`` check of their own,
so a handler that reaches one from a path outside the prefix is unauthenticated
in every identity mode. Nothing else in the tree would report that: the route
registers, the wire-contract snapshot lists its new path, and the service's own
unit tests keep passing because they call it directly.

This walks the constructed route table and attributes each handler to the
services it actually reaches, by the two wirings the route modules use:

* a service instance built in ``register_*_routes`` and captured in the
  handler's closure (``astrabox/api/routes/vaults.py`` and its siblings);
* a ``_svc().<method>`` call on the ``AgentPlatformService`` seam, resolved
  through that method's own body to the sub-service it delegates to
  (``astrabox/api/routes/admin.py``).

Attribution has to cover both, because ``AdminService`` is reachable only
through the seam and the other three only through a closure.
"""

from __future__ import annotations

import ast
import inspect
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import pytest

from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
from astrabox.web.identity_middleware import _is_admin_path

#: Services whose whole HTTP surface is administrative. A service belongs here
#: when both hold: every route that reaches it is under the admin prefix, and it
#: exposes operations a non-administrator must not perform (deployment
#: credentials, the organization MCP registry, cross-tenant session control).
#:
#: ``AssistantService`` and ``AgentPlatformService`` are deliberately absent —
#: both serve admin and non-admin routes, so the prefix is not their boundary
#: and their own ownership checks are. ``AgentExtensionService`` is absent for
#: the same reason: ``/api/v1/agents/{agent_id}/extensions`` is a normal Agent
#: manager surface, gated by that Agent's creator/admin policy.
_ADMIN_ONLY_SERVICES = frozenset(
    {
        "AdminService",
        "CredentialBindingService",
        "MCPRegistryService",
        "VaultService",
    }
)


# ── attribution of one handler to the services it reaches ────────────────────


@lru_cache(maxsize=None)
def _seam_delegation() -> dict[str, frozenset[str]]:
    """``AgentPlatformService`` method name → the sub-service classes it calls.

    Read from the seam's own source rather than a hand-written table, so a
    forward that is repointed at a different sub-service moves this map with
    it. ``self._admin_service`` is resolved to ``AdminService`` through the
    constructor assignment that builds it, which is either a direct
    ``VaultService()`` call or a ``self._construct(...)`` wrapping one.
    """
    classdef = ast.parse(inspect.getsource(AgentPlatformService)).body[0]
    assert isinstance(classdef, ast.ClassDef)

    attribute_class: dict[str, str] = {}
    for node in ast.walk(classdef):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            not isinstance(target, ast.Attribute)
            or not isinstance(target.value, ast.Name)
            or target.value.id != "self"
            or not target.attr.endswith("_service")
        ):
            continue
        for call in ast.walk(node.value):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id.endswith("Service")
            ):
                attribute_class[target.attr] = call.func.id
                break

    delegation: dict[str, frozenset[str]] = {}
    for node in classdef.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        delegation[node.name] = frozenset(
            attribute_class[sub.attr]
            for sub in ast.walk(node)
            if isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "self"
            and sub.attr in attribute_class
        )
    return delegation


@lru_cache(maxsize=None)
def _parsed_module(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def _handler_node(handler: Any) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The ``def`` for *handler*, located in its defining module by name.

    The module is parsed rather than ``inspect.getsource(handler)`` because a
    route handler is nested inside ``register_*_routes`` and its source arrives
    indented, which ``ast.parse`` rejects.
    """
    source_file = inspect.getsourcefile(handler)
    name = getattr(handler, "__name__", None)
    if not source_file or not name or not Path(source_file).is_file():
        return None
    for node in ast.walk(_parsed_module(source_file)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _closure_services(handler: Any) -> set[str]:
    """Service classes bound into *handler*'s closure by its register function."""
    services: set[str] = set()
    cells = getattr(handler, "__closure__", None) or ()
    for cell in cells:
        try:
            value = cell.cell_contents
        except ValueError:
            # A cell for a name the register function has not bound yet.
            continue
        class_name = type(value).__name__
        if class_name.endswith("Service"):
            services.add(class_name)
    return services


def _seam_services(handler: Any) -> set[str]:
    """Sub-service classes reached through ``_svc().<method>`` calls."""
    node = _handler_node(handler)
    if node is None:
        return set()
    delegation = _seam_delegation()
    services: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Call)
            and isinstance(sub.value.func, ast.Name)
            and sub.value.func.id == "_svc"
        ):
            services |= delegation.get(sub.attr, frozenset())
    return services


def _services_reached(handler: Any) -> set[str]:
    return _closure_services(handler) | _seam_services(handler)


# ── the constructed route table ──────────────────────────────────────────────


@contextmanager
def _deterministic_construction_env(frontend_dist: Path) -> Iterator[None]:
    """Pin the env ``create_app()`` reads, so local state cannot change which
    routes register. Mirrors ``tests/http_wire_contract_test.py``; an empty
    frontend directory keeps the shape API-only."""
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("ASTRABOX_ENV_FILE", str(frontend_dist.parent / "unused.env"))
        mp.setenv("ASTRABOX_WEB_IDENTITY", "local")
        mp.setenv("ASTRABOX_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
        mp.setenv("ASTRABOX_AGENT_ENABLED", "true")
        mp.setenv("ASTRABOX_FRONTEND_DIST", str(frontend_dist))
        mp.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def route_services(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, str, set[str]]]:
    """``(path, route name, services reached)`` for every constructed route."""
    frontend_dist = tmp_path_factory.mktemp("admin-prefix") / "no-frontend-dist"
    with _deterministic_construction_env(frontend_dist):
        from astrabox.api.app import create_app

        app = create_app()

    rows: list[tuple[str, str, set[str]]] = []

    def walk(routes: Any) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None) or getattr(route, "router", None)
            if inner is not None and getattr(inner, "routes", None) is not None:
                walk(inner.routes)
                continue
            path = getattr(route, "path", None)
            handler = getattr(route, "endpoint", None)
            if isinstance(path, str) and handler is not None:
                rows.append((path, str(getattr(route, "name", "") or ""), _services_reached(handler)))

    walk(app.routes)
    return rows


def _unguarded(rows: list[tuple[str, str, set[str]]]) -> list[str]:
    """Routes reaching an admin-only service from outside the gated prefix."""
    return [
        f"{path} [{name}] reaches {sorted(services & _ADMIN_ONLY_SERVICES)}"
        for path, name, services in rows
        if services & _ADMIN_ONLY_SERVICES and not _is_admin_path(path)
    ]


# ── the invariant ────────────────────────────────────────────────────────────


def test_admin_only_services_are_reachable_only_behind_the_admin_prefix(
    route_services: list[tuple[str, str, set[str]]],
) -> None:
    assert _unguarded(route_services) == [], (
        "these routes reach an administrator-only service from a path the "
        "admin gate in astrabox/web/identity_middleware.py does not cover, so "
        "any authenticated user can call them: "
        f"{_unguarded(route_services)}"
    )


def test_every_admin_only_service_is_actually_attributed_to_a_route(
    route_services: list[tuple[str, str, set[str]]],
) -> None:
    """Attribution that silently matches nothing would report every route clean.

    Each listed service must be found behind at least one route, so a change
    to how handlers reach services (a new registration style, a renamed seam
    accessor) fails here rather than turning the invariant above vacuous.
    """
    reached: set[str] = set()
    for _path, _name, services in route_services:
        reached |= services & _ADMIN_ONLY_SERVICES

    assert reached == _ADMIN_ONLY_SERVICES, (
        "no route was attributed to "
        f"{sorted(_ADMIN_ONLY_SERVICES - reached)} — the walk stopped seeing "
        "how handlers reach services, and the prefix check above now passes "
        "for a reason that has nothing to do with the routes being safe"
    )


def test_the_invariant_catches_an_admin_route_moved_out_of_the_prefix() -> None:
    """A vault route relocated off ``/api/v1/admin`` must be reported.

    Moving one is the concrete way the gate disappears: the handler and the
    service are untouched and every other check stays green, so this is the
    failure the module exists to produce.
    """
    moved = [("/api/v1/vaults/{vault_id}", "get_vault", {"VaultService"})]
    assert _unguarded(moved) == [
        "/api/v1/vaults/{vault_id} [get_vault] reaches ['VaultService']"
    ]

    stayed = [("/api/v1/admin/vaults/{vault_id}", "get_vault", {"VaultService"})]
    assert _unguarded(stayed) == []


def test_the_admin_api_automation_surface_counts_as_gated() -> None:
    """``/api/v1/admin-api/*`` is covered by the same prefix, with no slash.

    The middleware tests ``/api/v1/admin`` without a trailing separator so the
    automation surface is gated by the same rule; a check that assumed
    ``/api/v1/admin/`` would call every admin-api route a violation and be
    silenced rather than believed.
    """
    assert _is_admin_path("/api/v1/admin-api/sessions/all")
    assert _is_admin_path("/api/v1/admin/vaults")
    assert not _is_admin_path("/api/v1/vaults")
