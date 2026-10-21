"""Shared pytest fixtures for the unit suite."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator

import pytest

from tests._database import database, postgres_database  # noqa: F401 - shared store fixtures

_LIVE_E2E_CONTROLLER = False


def selects_live_e2e(config: pytest.Config) -> bool:
    """True when this invocation actually runs the live suite.

    Read from the marker expression rather than from file paths: the default in
    ``pyproject.toml`` is ``-m 'not e2e'`` and a live run overrides it with
    ``-m e2e``, so the sign of the ``e2e`` term is exactly the question.
    """
    expr = " ".join(str(getattr(config.option, "markexpr", "") or "").split())
    return "e2e" in expr and "not e2e" not in expr


def pytest_configure(config: pytest.Config) -> None:
    """Reject unsafe live scheduling and stop after the first failure.

    Live tests consume shared sandbox capacity, so later scenarios cannot add
    reliable diagnostic value after a failure. This top-level hook configures
    the xdist controller; a nested E2E conftest is loaded by workers during
    collection and cannot set the controller's ``maxfail``. An explicit
    ``--maxfail`` is overridden for the same capacity bound.

    A conftest cannot replace xdist's scheduler early enough to be reliable, so
    a live invocation with workers and any scheduler other than ``loadgroup``
    is a usage error. The canonical runner supplies the option; ad-hoc callers
    fail before collection instead of silently ignoring ``xdist_group`` marks.
    """
    global _LIVE_E2E_CONTROLLER
    live = selects_live_e2e(config)
    _LIVE_E2E_CONTROLLER = live and not hasattr(config, "workerinput")
    if not live:
        return
    if (
        getattr(config.option, "numprocesses", None)
        and getattr(config.option, "dist", "no") != "loadgroup"
    ):
        raise pytest.UsageError(
            "live e2e with xdist requires --dist loadgroup; "
            "run scripts/e2e_live.sh for the full suite"
        )
    if config.option.maxfail == 1:
        return
    config.option.maxfail = 1
    config.option.exitfirst = True
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            "live e2e: stopping at the first failure (fixed by tests/conftest.py; "
            "the failing test's sandbox is kept for diagnosis)",
            yellow=True,
        )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Persist live progress and announce the first failure as it happens.

    ``maxfail`` stops pytest DISPATCHING new tests, but the workers already
    running keep going — even the fixed 180 s per-test budget wastes a live
    cluster's capacity spent after the answer is already known. A wrapper script
    cannot see that moment in the output either: under ``-q`` the ``FAILED``
    lines are part of the end-of-run summary, so the only live signal is a
    progress character.

    So the controller drops a file the instant a report comes back failed, and
    ``scripts/e2e_live.sh`` watches for it. This hook runs on the CONTROLLER
    (worker reports are forwarded to it), which is what makes it usable as a
    cross-process signal at all.

    The controller also appends every setup/call/teardown report to
    ``ASTRABOX_E2E_REPORT_LOG``. Unlike terminal progress dots, that file
    survives the process-group kill and identifies exactly which tests still
    need evidence. Both outputs are off unless their paths are explicitly set,
    so a normal unit run is unchanged.
    """
    report_path = os.environ.get("ASTRABOX_E2E_REPORT_LOG", "").strip()
    if _LIVE_E2E_CONTROLLER and report_path:
        entry: dict[str, object] = {
            "duration": report.duration,
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "when": report.when,
        }
        if report.outcome == "failed":
            # Fail-fast termination can precede pytest's summary and JUnit
            # output, so retain the cause with this report. Bound its size
            # because a live assertion can include an entire message stream.
            entry["longrepr"] = str(report.longrepr)[-4000:]
        record = json.dumps(
            entry,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            encoded = f"{record}\n".encode()
            descriptor = os.open(
                report_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("short write")
            finally:
                os.close(descriptor)
        except OSError as exc:
            print(f"live e2e report log unavailable: {exc}", file=sys.stderr)

    if report.outcome != "failed":
        return
    path = os.environ.get("ASTRABOX_E2E_FAILURE_SENTINEL", "").strip()
    if not path:
        return
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(f"{report.nodeid}\t{report.when}\n")
            # The assertion itself, because the script aborts before pytest ever
            # prints one: under -q the failure block belongs to the end-of-run
            # summary, which an aborted run does not reach. Without this the
            # operator gets a test name and has to re-run to see why.
            handle.write(str(getattr(report, "longreprtext", "") or "")[-4000:])
    except FileExistsError:
        pass  # the first failure is the one that matters; later ones are noise
    except OSError:
        pass  # a signal that cannot be written must not fail the test run


@pytest.fixture(autouse=True)
def _default_transcript_signing_key() -> object:
    """Give the transcript capability a signing key by default.

    ``build_claude_options`` mints a transcript capability token whenever a
    callback base URL is configured, and a non-local deployment without a
    signing key fails closed. The general suite supplies a test key; cases that
    exercise the missing-key behavior clear it explicitly.
    """
    original = os.environ.get("ASTRABOX_TRANSCRIPT_SIGNING_KEY")
    os.environ["ASTRABOX_TRANSCRIPT_SIGNING_KEY"] = "unit-test-suite-signing-key"
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("ASTRABOX_TRANSCRIPT_SIGNING_KEY", None)
        else:
            os.environ["ASTRABOX_TRANSCRIPT_SIGNING_KEY"] = original


#: The installation id the general suite runs as. Outside tests the id is read
#: from the installation's database on first use; tests that create or reap
#: sandboxes without a database need it answered the same way.
TEST_SANDBOX_INSTALLATION_ID = "unit-test-installation"


@pytest.fixture(autouse=True)
def _composed_selections_are_restored() -> Iterator[None]:
    """Undo the enabled backend set and scheduler a test's ``bootstrap()`` selected.

    Both are process-wide. Left in place, a test that composed the default
    deployment would make every later test that registers a stand-in backend
    meet ``SANDBOX_BACKEND_DISABLED``, and every later test of an unselected
    scheduler see the default one.
    """
    from astrabox.seams import sandbox as sandbox_seam
    from astrabox.seams import scheduler as scheduler_seam

    enabled = sandbox_seam._ENABLED_BACKENDS
    scheduler = scheduler_seam._CONFIGURED
    try:
        yield
    finally:
        sandbox_seam._ENABLED_BACKENDS = enabled
        scheduler_seam._CONFIGURED = scheduler


@pytest.fixture(autouse=True)
def _sandbox_installation_id() -> Iterator[None]:
    """Answer :func:`sandbox_installation_id` with the suite's id, then restore."""
    from astrabox.seams import sandbox as sandbox_seam

    loader, cached = sandbox_seam._INSTALLATION_LOADER, sandbox_seam._INSTALLATION_ID

    async def _load() -> str:
        return TEST_SANDBOX_INSTALLATION_ID

    sandbox_seam.set_sandbox_installation_loader(_load)
    try:
        yield
    finally:
        sandbox_seam._INSTALLATION_LOADER = loader
        sandbox_seam._INSTALLATION_ID = cached


@pytest.fixture
def environment_member_accounts(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Declare fixture identities admitted to the serving Environment.

    Protocol, role and payload tests name their existing members explicitly.
    Live Environment acceptance exercises the real membership repository.
    """
    from astrabox.persistence.provider import get_persistence
    from astrabox.persistence.repository.environment_repository import EnvironmentRepository

    members: set[str] = set()

    async def has_member(_self: EnvironmentRepository, name: str, user_id: str) -> bool:
        return name == get_persistence().environment_name and user_id in members

    monkeypatch.setattr(EnvironmentRepository, "has_member", has_member)
    return members
