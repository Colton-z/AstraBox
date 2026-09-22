"""Agent routes resolve identity without delegating the caller's credential.

The configured identity provider verifies the browser or IdP bearer and returns
a ``UserContext``. Agent handlers must pass that identity to the service layer
without retaining or re-presenting the bearer as a managed Agent credential.

The AST checks enforce the structural boundary: the raw ``Request`` reaches
only the identity resolver, handlers read no credential-bearing request
attributes, and known delegation entry points remain absent. A live route check
also verifies that the caller's bearer is not reachable from arguments delivered
to the Agent service. This combination catches delegation regardless of the
name used for it while retaining a narrow check for known entry points.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROUTES_DIR = _REPO_ROOT / "astrabox" / "api" / "routes"
_AGENT_ROUTES = _ROUTES_DIR / "agents.py"

#: What may be handed the raw ``Request``. ``_resolve_user`` is the module's own
#: one-line wrapper; ``get_current_user_context`` is the seam it delegates to.
#: Anything else receiving the request object can read its Authorization header.
_REQUEST_MAY_REACH = frozenset({"_resolve_user", "get_current_user_context"})

#: Attributes a handler may read off the request. ``query_params`` carries no
#: credential; ``headers``, ``cookies``, and ``auth`` do.
_REQUEST_ATTRIBUTES_ALLOWED = frozenset({"query_params"})

#: Known delegation entry-point names that supplement the structural AST checks.
_DELEGATION_SYMBOL = re.compile(r"current_user_iam|sync_from_request")


def _module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _callees_given_the_request(tree: ast.Module) -> set[str]:
    """Names of every callee that receives ``request`` as an argument."""

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        supplied = [*node.args, *(keyword.value for keyword in node.keywords)]
        if not any(
            isinstance(argument, ast.Name) and argument.id == "request"
            for argument in supplied
        ):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            found.add(func.id)
        elif isinstance(func, ast.Attribute):
            found.add(func.attr)
        else:  # pragma: no cover - a call on an expression, not seen here
            found.add(ast.dump(func))
    return found


def _attributes_read_off_the_request(tree: ast.Module) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
    }


def test_the_raw_request_reaches_only_the_identity_resolver() -> None:
    """Handing the request anywhere else is how a bearer gets forwarded."""

    callees = _callees_given_the_request(_module_ast(_AGENT_ROUTES))
    assert callees, "the identity hop itself should have been found"
    unexpected = callees - _REQUEST_MAY_REACH
    assert not unexpected, (
        "astrabox/api/routes/agents.py hands the raw Request to "
        f"{sorted(unexpected)}. Agent routes resolve identity and pass the "
        "resulting UserContext onward; anything else given the request can read "
        "its Authorization header and re-present it as a second credential."
    )


def test_no_agent_handler_reads_a_credential_bearing_request_attribute() -> None:
    attributes = _attributes_read_off_the_request(_module_ast(_AGENT_ROUTES))
    unexpected = attributes - _REQUEST_ATTRIBUTES_ALLOWED
    assert not unexpected, (
        "astrabox/api/routes/agents.py reads request."
        f"{sorted(unexpected)}. Reading headers, cookies, or auth off the "
        "request is the step that makes IdP-token delegation possible; the "
        "verified UserContext is the route's input."
    )


def test_the_delegation_symbol_does_not_return_to_the_router_package() -> None:
    """Known delegation entry points remain absent from the router package."""

    offenders = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _ROUTES_DIR.rglob("*.py")
        if _DELEGATION_SYMBOL.search(path.read_text(encoding="utf-8"))
    )
    assert not offenders, (
        f"{offenders} reference the private-deployment IAM delegation removed "
        "in P1-26. Community authorization uses the verified UserContext."
    )


class _RecordingAgentService:
    """Stands in for the Agent service and remembers what the route passed it."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def list_agents(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append((args, kwargs))
        return []


def _mentions(value: Any, needle: str, seen: set[int] | None = None) -> bool:
    """Search a call's arguments, including object attributes, for ``needle``.

    Not ``repr``: ``UserContext`` inherits the default one, which prints an
    address and no state, so a token riding on the object it hands over would
    be invisible to a string search of the record. Anything reachable from what
    the service received is reachable by the service.
    """

    seen = set() if seen is None else seen
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, str):
        return needle in value
    if isinstance(value, (bool, int, float, type(None))):
        return False
    if isinstance(value, dict):
        return any(
            _mentions(key, needle, seen) or _mentions(item, needle, seen)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_mentions(item, needle, seen) for item in value)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return _mentions(dict(attributes), needle, seen)
    return needle in str(value)


def test_the_callers_bearer_never_reaches_the_agent_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The structural tests say the token is not taken; this says it does not arrive.

    Driven through the real app so the identity middleware, the route, and the
    service boundary are the ones under test.
    """

    bearer = "idp-token-that-must-not-be-delegated"
    service = _RecordingAgentService()
    monkeypatch.delenv("ASTRABOX_WEB_IDENTITY", raising=False)
    monkeypatch.delenv("ASTRABOX_AUTH_SESSION_SECRET", raising=False)
    monkeypatch.setenv("ASTRABOX_AGENT_ENABLED", "1")
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "astrabox.api.routes.agents.get_agent_service", lambda: service
    )

    from astrabox.config.settings import get_settings
    from astrabox.identity.session_signing import reset_session_signing_cache

    # Both values are process-scoped in production. This test changes their
    # environment inputs, so isolate it from whichever app test ran first.
    get_settings.cache_clear()
    reset_session_signing_cache()
    from astrabox.api.app import create_app

    try:
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/agents", headers={"Authorization": f"Bearer {bearer}"}
            )

        assert response.status_code == 200, response.text
        assert (tmp_path / "auth-session.key").is_file()
        assert service.calls, "the route should have reached the Agent service"
        assert not _mentions(service.calls, bearer), (
            "the caller's bearer reached the Agent service. The route must pass the "
            "verified UserContext only — a token that arrives here can be stored or "
            "re-presented upstream."
        )
    finally:
        reset_session_signing_cache()
        get_settings.cache_clear()
