from __future__ import annotations

import subprocess

import pytest

from tests.e2e._service_containers import (
    ServiceContainerHandle,
    ServiceContainerUnavailable,
    require_service_container,
    resolve_service_container,
)

HANDLE = ServiceContainerHandle(
    service="postgres",
    env_var="ASTRABOX_E2E_POSTGRES_CONTAINER",
    setup=(
        "Run ASTRABOX_E2E_POSTGRES_CONTAINER=<running-postgres-container> "
        ".venv/bin/python -m pytest tests/e2e/test_transcript_mirror.py -m e2e."
    ),
)


def result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


def test_explicit_handle_is_validated_without_compose_discovery() -> None:
    calls: list[list[str]] = []

    def run_docker(args: list[str]):
        calls.append(args)
        return result(stdout="true\n")

    container = resolve_service_container(
        HANDLE,
        environ={"ASTRABOX_E2E_POSTGRES_CONTAINER": "bare-postgres"},
        run_docker=run_docker,
    )

    assert container == "bare-postgres"
    assert calls == [
        ["inspect", "--format", "{{.State.Running}}", "bare-postgres"]
    ]


def test_unique_compose_service_can_be_discovered() -> None:
    container = resolve_service_container(
        HANDLE,
        environ={},
        run_docker=lambda _args: result(stdout="local-postgres-1\n"),
    )

    assert container == "local-postgres-1"


def test_missing_service_fails_setup_with_a_runnable_configuration() -> None:
    with pytest.raises(ServiceContainerUnavailable) as failure:
        require_service_container(
            HANDLE,
            environ={},
            run_docker=lambda _args: result(),
        )

    reason = str(failure.value)
    assert "Compose discovery found 0 running containers (none)" in reason
    assert "ASTRABOX_E2E_POSTGRES_CONTAINER=<running-postgres-container>" in reason
    assert "pytest tests/e2e/test_transcript_mirror.py -m e2e" in reason


def test_nonexistent_explicit_handle_fails_instead_of_skipping() -> None:
    with pytest.raises(RuntimeError, match="does not name a running Docker container"):
        require_service_container(
            HANDLE,
            environ={"ASTRABOX_E2E_POSTGRES_CONTAINER": "missing-postgres"},
            run_docker=lambda _args: result(
                returncode=1,
                stderr="Error: No such object: missing-postgres",
            ),
        )
