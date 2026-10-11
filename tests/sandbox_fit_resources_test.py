"""A box's resources are the platform's recipe, fitted by the backend creating it.

Docker refuses ``containers/create`` when the CPU limit exceeds the host's CPU
count, so on a two-CPU machine a fixed four-CPU ceiling left every fresh
installation unable to start a single conversation. Only the bundled
OpenSandbox server on the Docker runtime creates boxes on this host, so only
there does the limit come down to this host's CPUs: a Kubernetes limit may
exceed one node, and an existing OpenSandbox service runs on hosts this one
cannot see.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider
from astrabox.seams.sandbox import SandboxProvider


class _Plain(SandboxProvider):
    name = "plain-resources"

    def connection_config(self, **kwargs: Any) -> Any:
        return None

    def secret_material(self, *, settings: Any) -> str:
        return ""

    def build_dataplane(self, **kwargs: Any) -> Any:
        raise RuntimeError("no dataplane")

    async def connect(self, sandbox_id: str) -> Any:
        raise RuntimeError("no connect")

    async def kill(self, sandbox_id: str) -> bool:
        raise RuntimeError("no kill")


def test_a_backend_takes_the_recipe_as_given_unless_it_narrows_it() -> None:
    limits, requests = provisioning.sandbox_create_resources(shared=False)

    assert _Plain().fit_resources(limits, requests) == (limits, requests)
    assert limits["cpu"] == "1"


@pytest.fixture
def four_cpu_envelope(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A deployment that raised the CPU limit above a small host's count."""
    monkeypatch.setenv("ASTRABOX_SANDBOX_CPU_LIMIT", "4")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(("host_cpus", "expected"), [(2, "2"), (1, "1"), (16, "4")])
def test_the_bundled_docker_server_never_exceeds_the_host(
    monkeypatch: pytest.MonkeyPatch, four_cpu_envelope: Any, host_cpus: int, expected: str
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", "docker")
    monkeypatch.delenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: host_cpus)

    limits, requests = OpenSandboxSandboxProvider().fit_resources(
        *provisioning.sandbox_create_resources(shared=False)
    )

    assert limits["cpu"] == expected
    assert requests["cpu"] == "250m"


@pytest.mark.parametrize(
    ("runtime", "service_url"),
    [("kubernetes", None), ("docker", "https://sandbox-control.example.com")],
    ids=["kubernetes", "existing-service"],
)
def test_a_runtime_on_other_hosts_keeps_the_recipe(
    monkeypatch: pytest.MonkeyPatch, runtime: str, service_url: str | None
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", runtime)
    if service_url is None:
        monkeypatch.delenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", service_url)
    monkeypatch.setattr(os, "cpu_count", lambda: 2)

    limits, _ = OpenSandboxSandboxProvider().fit_resources(
        *provisioning.sandbox_create_resources(shared=False)
    )

    assert limits["cpu"] == "1"
