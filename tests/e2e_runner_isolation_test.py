"""Protect the live runner's deployment-restart isolation contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _REPO_ROOT / "scripts" / "e2e_live.sh"
_AGENT_POSTGRES_SCOPE = "not assistant_live"


def _fake_python(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-python"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time
            from pathlib import Path

            calls = Path(os.environ["FAKE_PYTEST_CALLS"])
            active = Path(os.environ["FAKE_PYTEST_ACTIVE"])
            overlap = False
            try:
                descriptor = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                overlap = True
            else:
                os.close(descriptor)

            with calls.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"args": sys.argv[1:], "overlap": overlap}) + "\\n")

            if overlap:
                raise SystemExit(91)
            try:
                time.sleep(0.05)
            finally:
                active.unlink(missing_ok=True)
            raise SystemExit(int(os.environ.get("FAKE_PYTEST_EXIT", "0")))
            """
        ),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_live_runner(
    tmp_path: Path,
    *,
    arguments: list[str] | None = None,
    fake_exit: int = 0,
    junit_dir: Path | None = None,
    lane: str = "main",
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pkill = fake_bin / "pkill"
    fake_pkill.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_pkill.chmod(0o755)

    calls = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ASTRABOX_E2E_BASE_URL": "http://unused.invalid",
        "ASTRABOX_E2E_PYTHON": str(_fake_python(tmp_path)),
        "E2E_WORKERS": "3",
        "E2E_LOG": str(tmp_path / "runner.log"),
        "E2E_LANE": lane,
        "FAKE_PYTEST_CALLS": str(calls),
        "FAKE_PYTEST_ACTIVE": str(tmp_path / "active"),
        "FAKE_PYTEST_EXIT": str(fake_exit),
    }
    if junit_dir is not None:
        env["ASTRABOX_E2E_JUNIT_DIR"] = str(junit_dir)
    result = subprocess.run(
        ["bash", str(_RUNNER), *(arguments or [])],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    records = (
        [json.loads(line) for line in calls.read_text().splitlines()]
        if calls.exists()
        else []
    )
    return result, records


def _pytest_mark_expression(args: list[str]) -> str:
    expressions = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == "-m"]
    return expressions[-1]


def test_restart_phase_begins_after_parallel_workers_exit(tmp_path: Path) -> None:
    result, records = _run_live_runner(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(records) == 2, records

    parallel, restart = records
    assert parallel["overlap"] is False
    assert _pytest_mark_expression(parallel["args"]) == (
        f"e2e and not backend_restart and {_AGENT_POSTGRES_SCOPE}"
    )
    assert parallel["args"][parallel["args"].index("-n") + 1] == "3"
    assert parallel["args"][parallel["args"].index("--dist") + 1] == "loadgroup"
    assert "--timeout=180" in parallel["args"]
    assert "--timeout-disable-debugger-detection" in parallel["args"]

    assert restart["overlap"] is False
    assert _pytest_mark_expression(restart["args"]) == (
        f"e2e and backend_restart and {_AGENT_POSTGRES_SCOPE}"
    )
    assert "-n" not in restart["args"]
    assert "--dist" not in restart["args"]
    assert "--timeout=180" in restart["args"]
    assert "--timeout-disable-debugger-detection" in restart["args"]


def test_each_main_phase_receives_an_independent_junit_path(tmp_path: Path) -> None:
    junit_dir = tmp_path / "junit"

    result, records = _run_live_runner(tmp_path, junit_dir=junit_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    parallel, restart = records
    assert f"--junitxml={junit_dir / 'parallel.xml'}" in parallel["args"]
    assert f"--junitxml={junit_dir / 'restart.xml'}" in restart["args"]
    assert junit_dir.is_dir()


def test_parallel_failure_never_enters_the_restart_window(tmp_path: Path) -> None:
    result, records = _run_live_runner(tmp_path, fake_exit=7)
    assert result.returncode == 7
    assert len(records) == 1, records
    assert _pytest_mark_expression(records[0]["args"]) == (
        f"e2e and not backend_restart and {_AGENT_POSTGRES_SCOPE}"
    )


def test_exact_node_selection_keeps_parallel_and_restart_isolated(
    tmp_path: Path,
) -> None:
    parallel_node = (
        "tests/e2e/test_delete_and_archive.py::test_delete_makes_session_unreadable"
    )
    restart_node = (
        "tests/e2e/test_reattach.py::test_sandbox_and_session_survive_backend_restart"
    )
    result, records = _run_live_runner(
        tmp_path,
        arguments=[
            "--parallel-node",
            parallel_node,
            "--restart-node",
            restart_node,
        ],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(records) == 2
    parallel, restart = records
    assert str(_REPO_ROOT / parallel_node) in parallel["args"]
    assert str(_REPO_ROOT / restart_node) not in parallel["args"]
    assert parallel["args"][parallel["args"].index("-n") + 1] == "3"
    assert str(_REPO_ROOT / restart_node) in restart["args"]
    assert str(_REPO_ROOT / parallel_node) not in restart["args"]
    assert "-n" not in restart["args"]


def test_assistant_lane_runs_once_without_an_empty_restart_phase(
    tmp_path: Path,
) -> None:
    result, records = _run_live_runner(tmp_path, lane="assistant")

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(records) == 1
    assert records[0]["overlap"] is False
    assert _pytest_mark_expression(records[0]["args"]) == (
        "e2e and assistant_live and not backend_restart"
    )
    assert "-n" not in records[0]["args"]
    assert "--timeout=180" in records[0]["args"]
    assert "--timeout-disable-debugger-detection" in records[0]["args"]


def test_removed_sqlite_lane_is_rejected_before_pytest(tmp_path: Path) -> None:
    result, records = _run_live_runner(tmp_path, lane="sqlite")

    assert result.returncode == 64
    assert records == []
    assert "expected main or assistant" in result.stderr


def test_unregistered_marker_is_a_collection_error(tmp_path: Path) -> None:
    generated = tmp_path / "unknown_marker_test.py"
    generated.write_text(
        "import pytest\n\n@pytest.mark.backend_restrat\ndef test_typo(): pass\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(_REPO_ROOT / "pyproject.toml"),
            str(generated),
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "backend_restrat" in output
