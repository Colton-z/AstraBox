"""The sandbox CPU ceiling fits the host whose Docker daemon runs the box.

Docker refuses ``containers/create`` when the CPU limit exceeds the host's CPU
count, so on a two-CPU machine a fixed four-CPU ceiling left every fresh
installation unable to start a single conversation. A Kubernetes limit may
exceed one node and keeps the platform's recipe.
"""

from __future__ import annotations

import os

import pytest

from astrabox.core.service.orchestrator.engine import provisioning


@pytest.mark.parametrize(("host_cpus", "expected"), [(2, "2"), (1, "1"), (16, "4")])
def test_docker_ceiling_never_exceeds_the_host(
    monkeypatch: pytest.MonkeyPatch, host_cpus: int, expected: str
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", "docker")
    monkeypatch.setattr(os, "cpu_count", lambda: host_cpus)

    limits, requests = provisioning.sandbox_create_resources()

    assert limits["cpu"] == expected
    assert requests["cpu"] == "200m"


def test_kubernetes_keeps_the_recipe_on_a_small_server_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", "kubernetes")
    monkeypatch.setattr(os, "cpu_count", lambda: 2)

    limits, _ = provisioning.sandbox_create_resources()

    assert limits["cpu"] == str(provisioning.SANDBOX_CPU_LIMIT)
