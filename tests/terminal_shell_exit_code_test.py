"""A shell-killing exit code survives the vendor surface that cannot carry it.

The vendor mechanism (execd isolated runs) learns a command's exit code from
an end-marker echoed AFTER the command in the session's persistent bash. A
command that takes bash with it — `exit 9`, or anything fatal to the shell —
kills that echo, and the vendor's reader returns a read error before its
exit-code capture (isolated_session_ctrl.go), so the surface reports an error
with no code at all. p87's first-ever run of the terminal e2e caught the
result: `exit 9` came back as exit_code=1 with the vendor's read error on
stderr.

The one channel that still speaks is bash's own EXIT trap, which runs before
the shell dies. The terminal wrapper installs it with the SAME status marker
the ordinary path prints, so the downstream parser needs no second code path.
"""

from __future__ import annotations

import re
import subprocess

from astrabox.core.service.orchestrator.terminal_service import (
    _isolated_terminal_code,
)

_IDENTITY = {
    "home_dir": "/tmp/poc-home",
    "linux_user": "conv_test",
    "workspace_dir": "/tmp",
}


def _render(command: str, marker: str = "__M__") -> str:
    return _isolated_terminal_code(
        command, cwd="/tmp", identity=dict(_IDENTITY), marker=marker
    )


def test_the_wrapper_installs_the_exit_trap_before_the_command() -> None:
    code = _render("echo hi")
    lines = code.splitlines()
    trap_at = next(i for i, line in enumerate(lines) if line.startswith("trap "))
    command_at = lines.index("echo hi")
    # Before the command, or a fatal first command fires no trap.
    assert trap_at < command_at
    # The trap prints the same marker the ordinary path prints, carrying $?.
    assert "__M__" in lines[trap_at]
    assert "$?" in lines[trap_at]


def test_a_shell_killing_exit_still_reports_its_own_code() -> None:
    """The violation that bit, executed for real.

    Run the rendered wrapper in an actual bash and parse what the terminal
    service's marker regex would parse. Without the trap this prints NO marker
    at all (the explicit printf follows the command, and `exit 9` never
    reaches it) — which is exactly how the live run produced a fabricated
    exit_code=1.
    """

    marker = "__M__"
    completed = subprocess.run(
        ["bash", "-c", _render("exit 9", marker)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    found = re.search(rf"{re.escape(marker)}:(-?\d+):", completed.stdout)
    assert found is not None, (
        f"no status marker survived the shell-killing exit: "
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    assert int(found.group(1)) == 9
    assert completed.returncode == 9


def test_an_ordinary_command_prints_exactly_one_marker() -> None:
    """The trap must not double-report.

    In production the wrapper runs inside a PERSISTENT bash, which does not
    exit after the chunk, so the trap never fires for an ordinary command.
    This exercises the wrapper in a bash that DOES exit afterwards — the
    worst case for double markers — and requires the parser's first match to
    be the explicit printf's true status, so even then the first-match
    consumer reads the right code.
    """

    marker = "__M__"
    completed = subprocess.run(
        ["bash", "-c", _render("false", marker)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    matches = re.findall(rf"{re.escape(marker)}:(-?\d+):", completed.stdout)
    assert matches, completed.stdout
    assert int(matches[0]) == 1
