"""/metrics exposure gate — unauthenticated by default, bearer-locked on demand.

The endpoint serves counter names + integers, so the default posture is the
usual open scrape endpoint (same trust class as ``/healthz``). A deployment
whose metrics port is reachable beyond its scrape network sets
``ASTRABOX_METRICS_TOKEN`` — from then on only ``Authorization: Bearer
<token>`` (Prometheus ``authorization.credentials``) may read it. These tests
drive the REAL app object (``create_app()``, no lifespan — the handler reads
env per request) through Starlette's in-process client.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from astrabox.api.app import create_app


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Neutralize identity extensions the same way the wire-contract test does:
    # the default (unset) resolver asserts nothing, so requests run as the
    # local user; no DB is touched by /metrics.
    monkeypatch.delenv("ASTRABOX_WEB_IDENTITY", raising=False)
    return TestClient(create_app())


def test_metrics_is_open_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRABOX_METRICS_TOKEN", raising=False)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_metrics_token_locks_out_missing_and_wrong_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_METRICS_TOKEN", "scrape-secret")
    assert client.get("/metrics").status_code == 401
    assert (
        client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    # A non-bearer scheme must not sneak through either.
    assert (
        client.get("/metrics", headers={"Authorization": "scrape-secret"}).status_code
        == 401
    )


def test_metrics_token_admits_the_bearer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_METRICS_TOKEN", "scrape-secret")
    response = client.get(
        "/metrics", headers={"Authorization": "Bearer scrape-secret"}
    )
    assert response.status_code == 200


def test_metrics_disabled_still_wins_over_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_METRICS_ENABLED", "false")
    monkeypatch.setenv("ASTRABOX_METRICS_TOKEN", "scrape-secret")
    assert (
        client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"}).status_code
        == 404
    )
