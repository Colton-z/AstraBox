"""Fixtures + marker wiring for the AstraBox Community e2e (live-turn) suite.

The ``e2e`` mark gates tests against a live deployment; they are **deselected by
default** so a unit CI run never reaches deployment infrastructure. Run them
explicitly with ``-m e2e``.

The ``e2e_client`` fixture connects only to ``ASTRABOX_E2E_BASE_URL``. Live
tests run against an already deployed stack so the database, credential edge,
and sandbox runtime are the product topology rather than a second host-only test
topology.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    OPEN_AGENTS,
    OPEN_ASSISTANTS,
    OPEN_SESSIONS,
    delete_session,
)

HEALTH_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_HEALTH_TIMEOUT", "60"))
MAX_E2E_TEST_SECONDS = 180
#: Left to a poll loop so it can refuse in its own words. pytest-timeout kills
#: the process where it stands, so a helper that polls right up to the ceiling
#: reports nothing at all — and what it had seen is the entire diagnostic.
LIVE_TEST_REPORTING_MARGIN_S = 25


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: live end-to-end turn against ASTRABOX_E2E_BASE_URL; deselected "
        "by default in unit CI.",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply one fixed timeout to every live test and reject local overrides."""
    for item in items:
        if item.get_closest_marker("e2e") is None:
            continue
        if item.get_closest_marker("timeout") is not None:
            raise pytest.UsageError(
                f"{item.nodeid} overrides the fixed {MAX_E2E_TEST_SECONDS}s live E2E timeout"
            )
        item.add_marker(pytest.mark.timeout(MAX_E2E_TEST_SECONDS))


@pytest.fixture
def live_test_deadline() -> float:
    """The monotonic time by which a polling helper must have given its verdict.

    The live E2E timeout is one fixed value for every test and deliberately not
    overridable, so a helper cannot be handed a duration: it has no way to know
    what the turn and the box kill already spent. It gets an absolute deadline
    from here instead, set to stop short of the ceiling.
    """
    return time.monotonic() + MAX_E2E_TEST_SECONDS - LIVE_TEST_REPORTING_MARGIN_S


def _wait_health(base_url: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"server /healthz not ready within {HEALTH_TIMEOUT_S:.0f}s")


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


@pytest.fixture(scope="session")
def e2e_auth_headers() -> dict[str, str]:
    """OIDC bearer used by API-only live tests; the value never enters logs."""
    raw = os.getenv("ASTRABOX_E2E_AUTH_TOKEN_FILE", "")
    path = Path(raw)
    _require(path.is_absolute(), "set ASTRABOX_E2E_AUTH_TOKEN_FILE to an absolute path")
    _require(path.is_file() and not path.is_symlink(), "E2E auth token must be a regular file")
    token = path.read_text(encoding="utf-8").strip()
    _require(bool(token), "E2E auth token file is empty")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def e2e_base_url() -> Iterator[str]:
    """A live backend base URL supplied by the AWS/deployment test harness."""
    external = os.getenv("ASTRABOX_E2E_BASE_URL", "").rstrip("/")
    _require(
        bool(external),
        "set ASTRABOX_E2E_BASE_URL to the deployment under test",
    )
    _wait_health(external)
    yield external


@pytest.fixture
def e2e_client(
    e2e_base_url: str,
    e2e_auth_headers: dict[str, str],
) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=e2e_base_url,
        headers=e2e_auth_headers,
        timeout=30.0,
    ) as client:
        yield client


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Publish each phase's report on the item so a fixture can read the outcome.

    A teardown fixture otherwise has no way to ask "did this test pass?", and
    that question decides whether the sandbox is rubbish or evidence.
    """
    outcome = yield
    setattr(item, f"_report_{call.when}", outcome.get_result())


def _failed(item: pytest.Item) -> bool:
    """True if any phase failed — setup and call both matter.

    A test that dies in setup never reaches its own ``finally``, but it may
    already have created a session; and a fixture error is exactly the case
    where the half-built box explains the most.
    """
    return any(
        getattr(getattr(item, f"_report_{phase}", None), "failed", False)
        for phase in ("setup", "call")
    )


@pytest.fixture(autouse=True)
def _reap_or_keep_sandboxes(
    request: pytest.FixtureRequest,
    e2e_base_url: str,
    e2e_auth_headers: dict[str, str],
) -> Iterator[None]:
    """Delete this test's sandboxes if it passed; leave them running if it did not.

    Retention depends on the result, so unexpected failures preserve their
    sandbox and partially created resources without requiring a pre-run flag.

    A retained prewarm-enabled Agent keeps replenishing capacity even after an
    individual box expires. After diagnosis, delete the exact resolved fixture
    Agent through the API so its prewarm pool retires with it; preserve open
    scenes that still need diagnosis.
    """
    OPEN_SESSIONS.clear()
    OPEN_ASSISTANTS.clear()
    OPEN_AGENTS.clear()
    yield
    sids = list(OPEN_SESSIONS)
    assistant_ids = list(OPEN_ASSISTANTS)
    agent_ids = list(OPEN_AGENTS)
    OPEN_SESSIONS.clear()
    OPEN_ASSISTANTS.clear()
    OPEN_AGENTS.clear()
    if not sids and not assistant_ids and not agent_ids:
        return

    if not _failed(request.node):
        with httpx.Client(
            base_url=e2e_base_url,
            headers=e2e_auth_headers,
            timeout=30.0,
        ) as client:
            for sid in sids:
                delete_session(client, sid)
            for assistant_id in assistant_ids:
                client.delete(f"/api/v1/assistants/{assistant_id}", timeout=60.0).raise_for_status()
            for agent_id in agent_ids:
                client.delete(f"/api/v1/agents/{agent_id}", timeout=60.0).raise_for_status()
        return

    # Name the scene where the reader will look for it: the report tail, next to
    # the assertion. A session id alone is not actionable — the sandbox id is
    # what identifies the runtime object, so resolve it while the session still answers.
    lines = [f"KEPT for diagnosis ({len(sids)} session(s)) — these were NOT deleted:"]
    with httpx.Client(
        base_url=e2e_base_url,
        headers=e2e_auth_headers,
        timeout=15.0,
    ) as client:
        for sid in sids:
            sandbox_id = ""
            try:
                detail = client.get(f"/api/v1/sessions/{sid}").json().get("data") or {}
                sandbox_id = str(detail.get("sandbox_id") or "")
            except Exception:
                pass
            lines.append(f"  session={sid} sandbox={sandbox_id or '<unresolved>'}")
    for assistant_id in assistant_ids:
        lines.append(f"  assistant={assistant_id} (workspace retained)")
    for agent_id in agent_ids:
        lines.append(f"  agent={agent_id} (configuration retained)")
    print("\n".join(lines))
