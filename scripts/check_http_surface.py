#!/usr/bin/env python3
"""The HTTP surface ``create_app()`` builds is the one the repository records.

Four rules, each a failure that registers and imports cleanly and then shows up
only on a request:

* **No two routes claim one (method, path).** Starlette resolves in
  registration order and the first match wins, so a collision silently shadows
  a handler.
* **Registration is deterministic.** Two independent constructions yield the
  same route table. Each ``register_*_routes(app)`` keys its idempotency guard
  off ``id(app)``, which CPython can reuse once an earlier app is collected; a
  second construction that registered fewer routes would show here first.
* **The committed OpenAPI snapshot is current.**
  ``tests/data/openapi_snapshot.json`` is what ``scripts/gen_openapi_snapshot.py``
  renders from the tree, so the console's generated client
  (``scripts/check_api_client.py``) describes the API the server serves.
* **Every ``_svc()`` call in a route resolves.** A handler body runs only when
  its route is called, so a ``_svc().<name>`` that ``AgentPlatformService`` does
  not define — as a class member or as an attribute its methods assign on
  ``self`` — fails at request time, not at import. Underscore names are skipped:
  routes reach those private attributes deliberately.

Run it directly:

    .venv/bin/python scripts/check_http_surface.py
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = REPO_ROOT / "astrabox" / "api" / "routes"

sys.path.insert(0, str(REPO_ROOT))


def _snapshot_generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_openapi_snapshot", REPO_ROOT / "scripts" / "gen_openapi_snapshot.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def effective_routes(app: Any) -> list[tuple[tuple[str, ...], str, str]]:
    """Every route the app registers, as ``(methods, path, name)``.

    ``include_router(...)`` keeps a lazy holder on the parent's ``.routes``
    rather than copying its children up, so the walk recurses into
    ``.original_router.routes``; the child paths are already fully qualified.
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
            rows.append((methods, path, str(getattr(route, "name", None) or "")))

    walk(app.routes)
    return rows


def collisions(rows: list[tuple[tuple[str, ...], str, str]]) -> list[str]:
    seen: dict[tuple[str, str], str] = {}
    found: list[str] = []
    for methods, path, name in rows:
        for method in methods:
            key = (method, path)
            if key in seen and seen[key] != name:
                found.append(f"{method} {path}: {seen[key]!r} vs {name!r}")
            else:
                seen[key] = name
    return found


def _assigned_on_self(cls: type) -> set[str]:
    """Attribute names the class's own methods assign on ``self``."""
    import inspect

    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(cls))):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)
    return names


def unresolved_service_calls() -> list[str]:
    from astrabox.core.service.orchestrator.platform_service import AgentPlatformService

    instance_attributes = _assigned_on_self(AgentPlatformService)
    found: list[str] = []
    calls = 0
    for module in sorted(ROUTES_DIR.glob("*.py")):
        if module.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_svc"
                and not node.attr.startswith("_")
            ):
                calls += 1
                if not hasattr(AgentPlatformService, node.attr) and (
                    node.attr not in instance_attributes
                ):
                    where = f"{module.relative_to(REPO_ROOT)}:{node.lineno}"
                    found.append(f"{where} _svc().{node.attr}")
    if not calls:
        # A walk that matches nothing would pass every route.
        found.append(f"no _svc() call found under {ROUTES_DIR.relative_to(REPO_ROOT)}")
    return found


def evaluate() -> list[tuple[str, list[str]]]:
    generator = _snapshot_generator()
    app = generator.build_app()
    rows = effective_routes(app)
    failures: list[tuple[str, list[str]]] = []

    def report(reason: str, items: list[str]) -> None:
        if items:
            failures.append((reason, items))

    report("claimed by two different routes", collisions(rows))
    second = sorted(effective_routes(generator.build_app()))
    if sorted(rows) != second:
        report(
            "registered by one construction and not the other",
            [f"{methods} {path} [{name}]" for methods, path, name in sorted(set(rows) ^ set(second))],
        )
    if generator.render_snapshot(app) != generator.SNAPSHOT_PATH.read_text(encoding="utf-8"):
        report(
            "out of date. Run `python scripts/gen_openapi_snapshot.py` and commit the "
            "result, then regenerate the console client (scripts/check_api_client.py)",
            [str(generator.SNAPSHOT_PATH.relative_to(REPO_ROOT))],
        )
    report(
        "called on AgentPlatformService, which does not define it",
        unresolved_service_calls(),
    )
    return failures


def main() -> int:
    failures = evaluate()
    for reason, items in failures:
        print(f"\n{len(items)} item(s) {reason}:")
        for item in items:
            print(f"  {item}")
    if failures:
        print("\nhttp surface FAILED")
        return 1
    print("http surface OK — routes unique and deterministic, snapshot current, service calls resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
