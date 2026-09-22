"""Verify that live E2E runs stop after one failure, including under xdist.

The top-level conftest owns ``maxfail`` because the xdist controller schedules
the workers and decides when the session stops. Non-E2E runs retain their
caller-supplied behavior.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.conftest import (
    pytest_configure,
    pytest_runtest_logreport,
    selects_live_e2e,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _Option:
    def __init__(
        self,
        markexpr: str,
        maxfail: int = 0,
        *,
        numprocesses: int | None = None,
        dist: str = "no",
    ) -> None:
        self.markexpr = markexpr
        self.maxfail = maxfail
        self.exitfirst = False
        self.numprocesses = numprocesses
        self.dist = dist


class _Config:
    def __init__(
        self,
        markexpr: str,
        maxfail: int = 0,
        *,
        numprocesses: int | None = None,
        dist: str = "no",
    ) -> None:
        self.option = _Option(
            markexpr,
            maxfail,
            numprocesses=numprocesses,
            dist=dist,
        )

    class _PM:
        @staticmethod
        def get_plugin(_name: str) -> None:
            return None

    pluginmanager = _PM()


_UNIT_EXPR = "not e2e and not mongo and not opensandbox"


def test_a_live_run_is_forced_to_stop_at_the_first_failure() -> None:
    config = _Config("e2e")
    pytest_configure(config)
    assert config.option.maxfail == 1
    assert config.option.exitfirst is True


def test_a_unit_run_is_left_alone() -> None:
    """The costliest way this could fail: unit runs must report everything.

    ``tests/conftest.py`` is loaded for every run in this repository, so a hook
    that keyed on being imported rather than on the marker expression would
    silently truncate the unit suite to its first failure.
    """
    config = _Config(_UNIT_EXPR)
    pytest_configure(config)
    assert config.option.maxfail == 0
    assert config.option.exitfirst is False
    assert config.option.dist == "no"


@pytest.mark.parametrize("supplied_dist", ["load", "loadscope", "worksteal"])
def test_live_xdist_rejects_schedulers_that_dissolve_groups(
    supplied_dist: str,
) -> None:
    """A caller choosing another scheduler must fail before live collection."""
    config = _Config("e2e", numprocesses=5, dist=supplied_dist)
    with pytest.raises(pytest.UsageError, match="requires --dist loadgroup"):
        pytest_configure(config)


@pytest.mark.parametrize(
    ("expr", "live"),
    [
        ("e2e", True),
        (" e2e ", True),
        ("e2e and not slow", True),
        (_UNIT_EXPR, False),
        ("not e2e", False),
        ("", False),
        ("mongo", False),
    ],
)
def test_the_marker_expression_decides(expr: str, live: bool) -> None:
    assert selects_live_e2e(_Config(expr)) is live


@pytest.mark.parametrize("supplied", [0, 5, 99])
def test_an_explicit_maxfail_does_not_win(supplied: int) -> None:
    """``--maxfail=5`` on a live run is the same mistake wearing a number."""
    config = _Config("e2e", maxfail=supplied)
    pytest_configure(config)
    assert config.option.maxfail == 1


def test_it_still_holds_under_xdist(tmp_path: Path) -> None:
    """The xdist controller stops scheduling after the first worker failure."""
    pytest.importorskip("xdist", reason="pytest-xdist is an e2e extra")
    # The REAL conftest, copied beside the throwaway suite so it is the rootdir
    # conftest of that tree — the same file, exercised through a real xdist run
    # rather than by calling its hook directly. It imports only `os` and
    # `pytest`, so a copy behaves identically.
    (tmp_path / "conftest.py").write_text((_REPO_ROOT / "tests" / "conftest.py").read_text())
    (tmp_path / "test_generated.py").write_text(textwrap.dedent(
        """
        import pytest

        pytestmark = [
            pytest.mark.e2e,
            pytest.mark.xdist_group("fail-fast-probe"),
        ]

        def test_a(): assert False
        def test_b(): assert False
        def test_c(): pass
        def test_d(): pass
        def test_e(): pass
        """
    ))
    report_log = tmp_path / "reports.jsonl"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", ".", "-m", "e2e", "-n", "2",
         "--dist", "loadgroup",
         "-p", "no:cacheprovider", "-q",
         "-W", "ignore::pytest.PytestUnknownMarkWarning"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "ASTRABOX_E2E_REPORT_LOG": str(report_log)},
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    executed = sum(
        int(n) for n, _ in re.findall(r"(\d+) (failed|passed)", out[-400:])
    )
    assert executed, f"could not read a pytest summary from:\n{out[-1500:]}"
    # Five were collected. The broken version ran all five ("2 failed, 3 passed");
    # a session that stops leaves at least one undispatched. Counting executed
    # tests keeps this about behaviour rather than about log wording — and the
    # second failure never running is the specific thing being bought.
    assert executed < 5, (
        "every collected test ran, so nothing stopped the session — this is "
        f"exactly how the tests/e2e/conftest.py version failed:\n{out[-1500:]}"
    )
    assert "2 failed" not in out, (
        f"the session reached a SECOND failure, so it did not stop at the first:\n{out[-1500:]}"
    )
    records = [json.loads(line) for line in report_log.read_text().splitlines()]
    identities = [
        (record["nodeid"], record["when"], record["outcome"])
        for record in records
    ]
    assert len(identities) == len(set(identities)), (
        "worker and controller both wrote the same report; the live ledger is ambiguous"
    )
    assert any(record["outcome"] == "failed" for record in records)


def test_live_xdist_without_loadgroup_fails_before_running(tmp_path: Path) -> None:
    """An ad-hoc parallel live run must not silently ignore isolation groups."""
    pytest.importorskip("xdist", reason="pytest-xdist is an e2e extra")
    (tmp_path / "conftest.py").write_text(
        (_REPO_ROOT / "tests" / "conftest.py").read_text()
    )
    executed = tmp_path / "executed"
    (tmp_path / "test_generated.py").write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path
            import pytest

            pytestmark = pytest.mark.e2e

            def test_must_not_run():
                Path({str(executed)!r}).write_text("ran")
            """
        )
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            ".",
            "-m",
            "e2e",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            "-q",
            "-W",
            "ignore::pytest.PytestUnknownMarkWarning",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == int(pytest.ExitCode.USAGE_ERROR), out
    assert "requires --dist loadgroup" in out
    assert not executed.exists(), "unsafe scheduler reached a live test body"


def test_the_failure_sentinel_fires_while_other_tests_are_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal `scripts/e2e_live.sh` aborts on, and its whole point: timing.

    ``maxfail`` stops pytest dispatching, but a worker already inside a live test
    can sit there for the full 180 s timeout. The sentinel has to land while that
    is still true, or the script has nothing to act on that beats simply waiting.

    So the probe deliberately pairs one fast failure with two long tests and
    asserts the file appears in a small fraction of their runtime.
    """
    pytest.importorskip("xdist", reason="pytest-xdist is an e2e extra")
    (tmp_path / "conftest.py").write_text((_REPO_ROOT / "tests" / "conftest.py").read_text())
    (tmp_path / "test_generated.py").write_text(textwrap.dedent(
        """
        import pytest, time

        pytestmark = pytest.mark.e2e

        def test_fails_fast(): assert False
        def test_slow_a(): time.sleep(30)
        def test_slow_b(): time.sleep(30)
        """
    ))
    sentinel = tmp_path / "failed.sentinel"
    env = {**os.environ, "ASTRABOX_E2E_FAILURE_SENTINEL": str(sentinel)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", ".", "-m", "e2e", "-n", "2",
         "--dist", "loadgroup",
         "-p", "no:cacheprovider", "-q",
         "-W", "ignore::pytest.PytestUnknownMarkWarning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=tmp_path, env=env, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline and not sentinel.exists():
            time.sleep(0.2)
        assert sentinel.exists(), (
            "no sentinel within 25s, while two 30s tests were still running — the "
            "script would have had to wait them out"
        )
        assert "test_fails_fast" in sentinel.read_text()
    finally:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=30)


def test_no_sentinel_path_means_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset is off: a unit run must not start writing files because a test failed."""
    monkeypatch.delenv("ASTRABOX_E2E_FAILURE_SENTINEL", raising=False)
    before = set(tmp_path.iterdir())
    report = type("R", (), {"outcome": "failed", "nodeid": "x::y", "when": "call"})()
    pytest_runtest_logreport(report)  # type: ignore[arg-type]
    assert set(tmp_path.iterdir()) == before


def test_live_controller_records_each_settled_phase_before_fail_fast_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_log = tmp_path / "reports.jsonl"
    monkeypatch.setenv("ASTRABOX_E2E_REPORT_LOG", str(report_log))
    pytest_configure(_Config("e2e"))
    for when in ("setup", "call", "teardown"):
        report = type(
            "R",
            (),
            {
                "duration": 0.25,
                "nodeid": "tests/e2e/test_live_turn.py::test_live_turn_streams_text_delta",
                "outcome": "passed",
                "when": when,
            },
        )()
        pytest_runtest_logreport(report)  # type: ignore[arg-type]

    records = [json.loads(line) for line in report_log.read_text().splitlines()]
    assert [record["when"] for record in records] == ["setup", "call", "teardown"]
    assert {record["outcome"] for record in records} == {"passed"}


def _sentinel_export_decision(setting: str | None) -> str:
    """Run the runner's own guard and report whether it armed the sentinel.

    Executes the lines from `e2e_live.sh` rather than asserting on their text,
    because what matters is the value the shell ends up with.
    """

    guard = (_REPO_ROOT / "scripts" / "e2e_live.sh").read_text(encoding="utf-8")
    start = guard.index('ABORT_ON_FIRST_FAILURE="${E2E_ABORT_ON_FIRST_FAILURE:-1}"')
    end = guard.index("TEST_TIMEOUT_S=")
    fragment = 'SENTINEL=/tmp/unused\n' + guard[start:end]
    script = fragment + '\necho "ARMED=${ASTRABOX_E2E_FAILURE_SENTINEL:-<unset>}"\n'
    environment = dict(os.environ)
    environment.pop("E2E_ABORT_ON_FIRST_FAILURE", None)
    if setting is not None:
        environment["E2E_ABORT_ON_FIRST_FAILURE"] = setting
    completed = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=environment
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_fail_fast_is_what_an_unset_switch_means() -> None:
    """The default must stay abort-on-first-failure.

    A survey switch that defaulted the other way would turn every ordinary run
    into one that spends the whole lane after the answer is known, and nothing
    else in the pipeline would report that it had.
    """

    assert "ARMED=/tmp/unused" in _sentinel_export_decision(None)
    assert "ARMED=/tmp/unused" in _sentinel_export_decision("1")


def test_survey_mode_leaves_the_sentinel_unarmed() -> None:
    """Off means the abort watcher never has a file to find, so nothing kills the run."""

    output = _sentinel_export_decision("0")
    assert "ARMED=<unset>" in output
    assert "SURVEY MODE" in output


def test_a_failed_report_carries_its_reason_not_just_its_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record has to answer "why", because nothing else survives.

    Stopping at the first failure kills the run before pytest writes its
    summary or its junit file, so this line is the whole record of what
    happened. A passing report stays lean — the reason only exists for a
    failure, and every green line would otherwise carry an empty field.
    """

    report_log = tmp_path / "reports.jsonl"
    monkeypatch.setenv("ASTRABOX_E2E_REPORT_LOG", str(report_log))
    pytest_configure(_Config("e2e"))

    passed = type(
        "R",
        (),
        {
            "duration": 0.5,
            "nodeid": "tests/e2e/test_live_turn.py::test_ok",
            "outcome": "passed",
            "when": "call",
            "longrepr": None,
        },
    )()
    failed = type(
        "R",
        (),
        {
            "duration": 97.3,
            "nodeid": "tests/e2e/test_shared_box_cohabitation.py::test_two",
            "outcome": "failed",
            "when": "call",
            "longrepr": "E   AssertionError: the sibling lost its workspace",
        },
    )()
    pytest_runtest_logreport(passed)  # type: ignore[arg-type]
    pytest_runtest_logreport(failed)  # type: ignore[arg-type]

    records = [json.loads(line) for line in report_log.read_text().splitlines()]
    assert "longrepr" not in records[0]
    assert "the sibling lost its workspace" in records[1]["longrepr"]


def test_a_reason_too_large_to_read_is_cut_to_its_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live repr embeds whole streams; the assertion is at the end of it."""

    report_log = tmp_path / "reports.jsonl"
    monkeypatch.setenv("ASTRABOX_E2E_REPORT_LOG", str(report_log))
    pytest_configure(_Config("e2e"))

    report = type(
        "R",
        (),
        {
            "duration": 1.0,
            "nodeid": "tests/e2e/test_live_turn.py::test_big",
            "outcome": "failed",
            "when": "call",
            "longrepr": ("x" * 20000) + "E   AssertionError: the last line",
        },
    )()
    pytest_runtest_logreport(report)  # type: ignore[arg-type]

    record = json.loads(report_log.read_text().splitlines()[0])
    assert len(record["longrepr"]) == 4000
    assert record["longrepr"].endswith("E   AssertionError: the last line")
