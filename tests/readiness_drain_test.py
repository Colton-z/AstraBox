"""/readyz + drain state for rolling deploys.

/readyz flips to NOT-ready the moment shutdown begins (drain or quiesce), so
the load balancer stops routing new traffic to a draining replica while its
in-flight turns finish; /healthz stays live through the drain.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import astrabox.api.readiness as readiness


@pytest.fixture(autouse=True)
def _clean_drain():
    readiness.reset_drain_for_tests()
    yield
    readiness.reset_drain_for_tests()


def test_ready_by_default() -> None:
    ready, reason = readiness.readiness_status()
    assert ready is True and reason == "ready"


def test_begin_drain_flips_to_not_ready() -> None:
    readiness.begin_drain()
    ready, reason = readiness.readiness_status()
    assert ready is False and reason == "draining"


def test_quiesced_platform_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import astrabox.core.service.orchestrator.service_registry as reg

    monkeypatch.setattr(
        reg, "get_platform_service",
        lambda: SimpleNamespace(quiesced_reason="lifespan_shutdown"),
    )
    ready, reason = readiness.readiness_status()
    assert ready is False and reason.startswith("quiesced:")


def test_no_platform_yet_is_still_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    import astrabox.core.service.orchestrator.service_registry as reg

    def _boom():
        raise RuntimeError("not bootstrapped")

    monkeypatch.setattr(reg, "get_platform_service", _boom)
    ready, _reason = readiness.readiness_status()
    assert ready is True, "an un-bootstrapped platform must not fail readiness"


def test_reconcile_constants_are_env_overridable() -> None:
    program = """
from astrabox.core.service.orchestrator.session_kernel.workers import reconcile_worker

print(reconcile_worker.SCAN_INTERVAL_S, reconcile_worker.HEARTBEAT_STALE_S)
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        env={
            **os.environ,
            "ASTRABOX_RECONCILE_SCAN_INTERVAL_S": "3",
            "ASTRABOX_RECONCILE_HEARTBEAT_STALE_S": "7",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3.0 7.0"
