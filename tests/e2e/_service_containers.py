"""Deployment-service handles for live E2E tests that run on a Docker host."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

DockerResult = subprocess.CompletedProcess[str]
DockerRunner = Callable[[list[str]], DockerResult]


@dataclass(frozen=True)
class ServiceContainerHandle:
    """How an E2E prerequisite maps to an explicit container or Compose service."""

    service: str
    env_var: str
    setup: str = ""


class ServiceContainerUnavailable(RuntimeError):
    """The deployment did not expose a handle and Compose discovery found none."""


POSTGRES_CONTAINER_HANDLE = ServiceContainerHandle(
    service="postgres",
    env_var="ASTRABOX_E2E_POSTGRES_CONTAINER",
    setup=(
        "Rerun the selected test as "
        "`ASTRABOX_E2E_POSTGRES_CONTAINER=<running-postgres-container> "
        ".venv/bin/python -m pytest <test-file> -m e2e`; replace <test-file> "
        "with the test being run."
    ),
)
SERVER_CONTAINER_HANDLE = ServiceContainerHandle(
    service="server",
    env_var="ASTRABOX_E2E_SERVER_CONTAINER",
    setup=(
        "Rerun the selected test as "
        "`ASTRABOX_E2E_SERVER_CONTAINER=<running-server-container> "
        ".venv/bin/python -m pytest <test-file> -m e2e`; replace <test-file> "
        "with the test being run."
    ),
)


def _docker(args: list[str]) -> DockerResult:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _setup_instructions(handle: ServiceContainerHandle) -> str:
    if handle.setup:
        return handle.setup
    return (
        f"Set {handle.env_var}=<running-{handle.service}-container> on the test command. "
        "The value must be the exact name or ID of a running container visible to this "
        "host's Docker daemon."
    )


def _diagnostic(result: DockerResult) -> str:
    return result.stderr.strip() or f"exit status {result.returncode}"


def resolve_service_container(
    handle: ServiceContainerHandle,
    *,
    environ: Mapping[str, str] | None = None,
    run_docker: DockerRunner = _docker,
) -> str:
    """Resolve and validate a deployment service without requiring Compose."""
    env = os.environ if environ is None else environ
    configured = str(env.get(handle.env_var) or "").strip()

    if configured:
        try:
            inspected = run_docker(
                ["inspect", "--format", "{{.State.Running}}", configured]
            )
        except Exception as exc:
            raise RuntimeError(
                f"{handle.env_var}={configured!r} could not be validated as a running "
                f"Docker container for the {handle.service} service: {exc}. "
                f"{_setup_instructions(handle)}"
            ) from exc
        if inspected.returncode != 0 or inspected.stdout.strip() != "true":
            raise RuntimeError(
                f"{handle.env_var}={configured!r} does not name a running Docker container "
                f"for the {handle.service} service: {_diagnostic(inspected)}. "
                f"{_setup_instructions(handle)}"
            )
        return configured

    try:
        discovered = run_docker(
            [
                "ps",
                "--filter",
                f"label=com.docker.compose.service={handle.service}",
                "--format",
                "{{.Names}}",
            ]
        )
    except Exception as exc:
        raise ServiceContainerUnavailable(
            f"{handle.service} service is unavailable to this E2E harness: "
            f"{handle.env_var} is unset and Compose discovery failed ({exc}). "
            f"{_setup_instructions(handle)}"
        ) from exc
    if discovered.returncode != 0:
        raise ServiceContainerUnavailable(
            f"{handle.service} service is unavailable to this E2E harness: "
            f"{handle.env_var} is unset and Compose discovery failed "
            f"({_diagnostic(discovered)}). {_setup_instructions(handle)}"
        )

    matches = [line.strip() for line in discovered.stdout.splitlines() if line.strip()]
    if len(matches) != 1:
        names = ", ".join(matches) or "none"
        raise ServiceContainerUnavailable(
            f"{handle.service} service is unavailable to this E2E harness: "
            f"{handle.env_var} is unset and Compose discovery found {len(matches)} "
            f"running containers ({names}). {_setup_instructions(handle)}"
        )
    return matches[0]


def require_service_container(
    handle: ServiceContainerHandle,
    *,
    environ: Mapping[str, str] | None = None,
    run_docker: DockerRunner = _docker,
) -> str:
    """Resolve a required service or fail setup with actionable instructions."""
    return resolve_service_container(handle, environ=environ, run_docker=run_docker)
