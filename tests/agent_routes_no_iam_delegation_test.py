"""Agent routes resolve identity without delegating the caller's credential.

The configured identity provider verifies the browser or IdP bearer and returns
a ``UserContext``. Agent handlers must pass that identity to the service layer
without retaining or re-presenting the bearer as a managed Agent credential.

A live route check verifies that the caller's bearer is not reachable from
arguments delivered to the Agent service, whatever name a delegation would use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


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


# The app's startup upgrades the PostgreSQL schema before it serves a request.
@pytest.mark.usefixtures("database")
def test_the_callers_bearer_never_reaches_the_agent_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: str,
) -> None:
    """The structural tests say the token is not taken; this says it does not arrive.

    Driven through the real app so the identity middleware, the route, and the
    service boundary are the ones under test. The app's startup reads the
    stored Deployments, so it runs against the test database.
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
