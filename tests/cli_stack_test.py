"""``astrabox up``/``status`` — preflight, the bounded wait, and its verdict.

Compose itself is not exercised here; what is, is the part this CLI adds: it
must refuse loudly when it cannot start anything, and it must never end a start
without saying whether the deployment came up.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.cli import stack
from astrabox.cli.output import EXIT_UNREACHABLE, EXIT_USAGE, CliError


@pytest.fixture(autouse=True)
def _isolated_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ASTRABOX_ENDPOINT",
        "ASTRABOX_SERVER_HOST_PORT",
        "ASTRABOX_TOKEN",
        "ASTRABOX_CLIENT_ID",
        "ASTRABOX_CLIENT_SECRET",
        "ASTRABOX_TOKEN_URL",
        "ASTRABOX_SCOPE",
        "ASTRABOX_AGENT_IMAGE",
    ):
        monkeypatch.delenv(name, raising=False)


def _checkout(tmp_path: Path) -> Path:
    """A directory carrying both markers `_repository_root` looks for."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "containers").mkdir()
    (tmp_path / "scripts" / "compose.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "containers" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return tmp_path


def test_a_directory_without_the_stack_definition_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Compose stack's definition, entry point and mounted material all
    live in the repository, so there is nothing to start without one. Naming
    the missing markers beats a Compose error about a missing file."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(CliError) as caught:
        stack._repository_root()

    assert caught.value.exit_code == EXIT_USAGE
    assert "scripts/compose.sh" in caught.value.details["expected"]


def test_the_checkout_is_found_from_a_subdirectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _checkout(tmp_path)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert stack._repository_root() == root


def test_a_missing_docker_is_named_rather_than_left_to_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(CliError) as caught:
        stack._docker_server_version()

    assert caught.value.exit_code == EXIT_USAGE
    assert "docker" in caught.value.message


def test_a_dead_docker_daemon_is_distinct_from_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two need different fixes, and Compose reports both as a connection
    error."""
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="daemon not running"),
    )

    with pytest.raises(CliError) as caught:
        stack._docker_server_version()

    assert "daemon not running" in caught.value.message


def test_the_wait_fails_loud_with_what_the_probe_kept_seeing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that returns without a verdict leaves a caller unable to tell a
    deployment still initialising from one that died."""
    monkeypatch.setattr(stack, "probe", lambda *_a, **_k: (False, "Connection refused"))

    with pytest.raises(CliError) as caught:
        stack._wait_until_ready("http://deployment.test", deadline_seconds=0)

    assert caught.value.exit_code == EXIT_UNREACHABLE
    assert caught.value.details["endpoint"] == "http://deployment.test"


def test_the_wait_returns_once_the_deployment_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stack, "probe", lambda *_a, **_k: (True, "HTTP 200"))

    assert stack._wait_until_ready("http://deployment.test", deadline_seconds=30) is True


def test_status_on_nothing_reports_the_address_it_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller retrying a wrong address forever is what the unreachable exit
    code prevents."""
    monkeypatch.setattr(stack, "probe", lambda *_a, **_k: (False, "Connection refused"))

    with pytest.raises(CliError) as caught:
        stack._cmd_status(_args())

    assert caught.value.exit_code == EXIT_UNREACHABLE
    assert caught.value.details["endpoint"] == "http://deployment.test"


def test_status_reports_health_and_readiness_separately(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A replica draining answers /healthz and refuses /readyz; collapsing the
    two would call that deployment down."""
    answers = {"/healthz": (True, "HTTP 200"), "/readyz": (False, "HTTP 503")}
    monkeypatch.setattr(stack, "probe", lambda _base, path: answers[path])

    assert stack._cmd_status(_args()) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["ready"] is False


def test_up_reports_a_missing_agent_image_without_refusing_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """An Environment may pin an image of its own or pull one, so a missing
    local image is not always wrong — but it is the usual reason a deployment
    starts cleanly and then cannot open a session."""
    monkeypatch.chdir(_checkout(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(stack, "_docker_server_version", lambda: "27.0.0")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_k: SimpleNamespace(returncode=1 if "inspect" in argv else 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(stack, "_wait_until_ready", lambda *_a, **_k: True)

    assert stack._cmd_up(_args(build=False)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["agent_image"]["present"] is False
    assert payload["ready"] is True


def test_a_failing_compose_run_is_a_loud_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=2, stdout="", stderr="")
    )

    with pytest.raises(CliError) as caught:
        stack._compose(root, ["up", "-d"], output="table")

    assert "exited 2" in caught.value.message


def _args(**overrides: Any) -> Any:
    args = {
        "endpoint": "http://deployment.test",
        "token": None,
        "output": "json",
        "build": False,
        "wait_seconds": 30,
        "no_wait": False,
        "volumes": False,
        "service": None,
        "tail": "200",
        "follow": False,
    }
    args.update(overrides)
    return SimpleNamespace(**args)
