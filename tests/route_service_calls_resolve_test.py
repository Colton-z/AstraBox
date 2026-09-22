"""Every route ``_svc()`` call resolves across the platform-service seam.

Route registration does not execute handler bodies, and direct service tests do
not prove that ``AgentPlatformService`` exposes the same method. This module
walks route source and verifies that each public attribute called on a bare
``_svc()`` result exists on ``AgentPlatformService``. Private instance
attributes and dynamically constructed calls remain outside this class-level
AST check.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from astrabox.core.service.orchestrator.platform_service import AgentPlatformService

ROUTES_DIR = pathlib.Path(__file__).resolve().parent.parent / "astrabox" / "api" / "routes"


def _svc_attribute_calls(source: str) -> set[str]:
    """Public names read off the result of a bare ``_svc()`` call.

    Underscore names are excluded because the routes that use them reach
    instance attributes bound in ``__init__``. They are invisible to this
    class-level check and require a separate service-boundary check.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_svc"
            and not node.attr.startswith("_")
        ):
            found.add(node.attr)
    return found


def _route_modules() -> list[pathlib.Path]:
    return sorted(p for p in ROUTES_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("module", _route_modules(), ids=lambda p: p.name)
def test_every_service_call_in_a_route_resolves(module: pathlib.Path) -> None:
    wanted = _svc_attribute_calls(module.read_text(encoding="utf-8"))
    missing = sorted(name for name in wanted if not hasattr(AgentPlatformService, name))
    assert not missing, (
        f"{module.name} calls _svc().{{{', '.join(missing)}}}, which "
        f"AgentPlatformService does not define. A handler body only runs when the "
        f"route is called, so this fails at request time, not at import."
    )


def test_the_check_can_see_a_missing_forward() -> None:
    """A synthetic missing forward proves that the source walk detects calls."""
    names = _svc_attribute_calls("async def h(r):\n    return await _svc().not_a_real_method(r)\n")

    assert names == {"not_a_real_method"}
    assert not hasattr(AgentPlatformService, "not_a_real_method")


def test_the_check_actually_found_calls_to_examine() -> None:
    """Guard against the walk matching nothing and passing vacuously."""
    seen = set()
    for module in _route_modules():
        seen |= _svc_attribute_calls(module.read_text(encoding="utf-8"))

    assert len(seen) > 20, f"only {len(seen)} _svc() calls found — the walk is not seeing routes"
