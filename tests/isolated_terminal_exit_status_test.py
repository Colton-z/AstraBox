"""A terminal reports the shell's exit status, or says it cannot determine one.

execd represents a nonzero shell exit as ``CommandExecError`` with its code in
``evalue``. A later transport error while draining output is secondary evidence
and must not replace that already-known process status with a synthetic code.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrabox.providers.open_sandbox.sandbox import ReportedExitStatus


def test_the_exit_status_survives_a_transport_error_behind_it() -> None:
    status = ReportedExitStatus()

    # execd's own order: the shell's exit status, then the drain complaining
    # about the pipe that shell closed.
    status.record(SimpleNamespace(name="CommandExecError", value="9"))
    status.record(
        SimpleNamespace(
            name="RuntimeError", value="read stdout: read |0: file already closed"
        )
    )

    assert status.code == 9


def test_a_transport_error_alone_names_no_exit_status() -> None:
    """The control: with nothing numeric on the wire, nothing is invented.

    Without this, the case above would pass just as well against a keeper that
    answered 9 to anything, and the caller would be back to reading a number
    that was never reported.
    """
    status = ReportedExitStatus()

    status.record(
        SimpleNamespace(
            name="RuntimeError", value="read stdout: read |0: file already closed"
        )
    )

    assert status.code is None


@pytest.mark.parametrize("first", ["0", "137"])
def test_the_first_status_wins_over_a_later_one(first: str) -> None:
    """A second numeric error is a second event, not a correction.

    The process exits once; whatever execd says afterwards describes the
    plumbing around it. Zero is included because it is the value most easily
    lost to a truthiness test.
    """
    status = ReportedExitStatus()

    status.record(SimpleNamespace(name="CommandExecError", value=first))
    status.record(SimpleNamespace(name="CommandExecError", value="42"))

    assert status.code == int(first)
