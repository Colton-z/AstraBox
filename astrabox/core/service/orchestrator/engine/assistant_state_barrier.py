"""Host-side steps an Assistant runtime adapter drives its box through.

An Assistant runtime is a resident program the image supervises with
supervisord, and the platform keeps the runtime's native state by having the
image's state mirror force a save before the box is given up. These are the
engine-neutral steps of that work. Everything the engine owns is the adapter's
to pass: program names, the lock and marker paths, the save command and its
marker, the readiness probe and the wording of each failure.

* :func:`await_command_channel`: the box's command service runs commands.
* :func:`close_supervised_restarts`, then :func:`stop_supervised_program` for
  each state writer: the start of ``quiesce_and_save_runtime_state``.
* :func:`confirm_forced_save`: the image's forced save and the snapshot it
  reports.
* :func:`restart_resident_program`: a restart for changed process inputs,
  excluded from a pause by the same lock.
* :func:`wait_until_answering`: an in-box readiness wait with a budget.
"""

from __future__ import annotations

import asyncio
import math
import posixpath
import re
import shlex
import time
from typing import Any, Callable

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.runtime import RuntimeProtocolError
from astrabox.seams.sandbox_data import SandboxCommands

#: How long the box's command service may take to run its first command once
#: the box has started.
COMMAND_CHANNEL_TIMEOUT_S = 90
#: The exit status by which a guarded restart reports that a pause has begun.
_RESTART_REFUSED_EXIT = 75
#: The exit status by which the readiness loop reports an answer the runtime
#: never gives.
_FOREIGN_ANSWER_EXIT = 3


async def await_command_channel(sandbox: Any, *, engine_label: str) -> None:
    """Block until the box's command service runs a command, or fail loud.

    Each attempt is a no-op through the box's commands face with a five-second
    deadline of its own, a second apart, until :data:`COMMAND_CHANNEL_TIMEOUT_S`
    has passed. Call it from ``activate_runtime``, after the platform tracks
    the box, rather than while the box is created: a timeout then reaches the
    platform's startup rollback (``RuntimeManager._rollback_runtime_start``)
    instead of leaving a started box that nothing tracks.

    Raises ``SANDBOX_INBOX_SERVER_UNREADY`` (502), its message naming
    ``engine_label`` and the last attempt's failure.
    """
    deadline = time.monotonic() + float(COMMAND_CHANNEL_TIMEOUT_S)
    last_exc: BaseException | None = None
    while True:
        try:
            await sandbox.commands.run("true", timeout=5.0)
            return
        except Exception as exc:  # noqa: BLE001 — box still booting; retry to deadline
            last_exc = exc
        if time.monotonic() >= deadline:
            raise APIError(
                code="SANDBOX_INBOX_SERVER_UNREADY",
                message=(
                    f"{engine_label} command service did not become ready within "
                    f"{COMMAND_CHANNEL_TIMEOUT_S}s: {last_exc}"
                ),
                status_code=502,
            )
        await asyncio.sleep(1.0)


def _supervise_directory(lock: str, marker: str) -> str:
    """The directory ``lock`` and ``marker`` share.

    A pause and a restart each create only this directory before taking the
    lock in it, so a lock kept in another directory could be missing when a
    box pauses before any restart has run. Raises :class:`ValueError` for two
    directories.
    """
    directory = posixpath.dirname(marker)
    if posixpath.dirname(lock) != directory:
        raise ValueError(
            f"supervise lock {lock!r} and pausing marker {marker!r} are not in one directory"
        )
    return directory


async def close_supervised_restarts(
    commands: SandboxCommands, *, lock: str, marker: str
) -> None:
    """End resident-program restarts in the box: the first step of a pause.

    Writes ``marker`` while holding ``lock``, the lock
    :func:`restart_resident_program` restarts under, so a restart already
    running finishes first and none starts after. The command has no deadline:
    it waits out a restart holding the lock, which supervisor's stop wait
    bounds.

    Raises :class:`ValueError` when ``lock`` and ``marker`` are not in one
    directory, and :class:`RuntimeError` when the box does not record the
    marker.
    """
    directory = _supervise_directory(lock, marker)
    closed = await commands.run(
        "mkdir -p " + shlex.quote(directory)
        + " && exec flock " + shlex.quote(lock)
        + " touch " + shlex.quote(marker),
        timeout=None,
    )
    if closed.exit_code != 0:
        raise RuntimeError("the box did not record that its restarts are closed")


async def stop_supervised_program(commands: SandboxCommands, program: str) -> None:
    """Stop supervisord's ``program`` and confirm from supervisor that it is not running.

    Call it after :func:`close_supervised_restarts`; before that, a restart
    can start the program again. A program that failed can already have
    exited, so the confirmation is supervisor's read-back rather than the stop
    command's result: the program's status must read ``STOPPED``, ``EXITED``
    or ``FATAL``.

    Raises :class:`RuntimeError` when supervisor does not confirm it.
    """
    await commands.run("supervisorctl stop " + shlex.quote(program))
    status = await commands.run(
        "bash -lc " + shlex.quote(
            "status=0; supervisorctl status " + shlex.quote(program)
            + '; status=$?; if [ "$status" -ne 0 ] '
            '&& [ "$status" -ne 3 ]; then exit "$status"; fi'
        ),
    )
    if (
        status.exit_code != 0
        or re.fullmatch(
            re.escape(program) + r"\s+(?:STOPPED|EXITED|FATAL)(?:\s+[^\n]*)?",
            status.output.strip(),
        ) is None
    ):
        raise RuntimeError("supervisor did not confirm a stopped process")


async def confirm_forced_save(
    commands: SandboxCommands, command: str, marker_re: re.Pattern[str]
) -> None:
    """Run the image's forced state save and confirm the snapshot it reports.

    ``command`` runs the mirror's ``save`` as the profile's account. It has no
    deadline, since a save takes as long as the native state is large. The save
    is confirmed when the command exits 0 and exactly one line of its output
    matches ``marker_re`` in full.

    Raises :class:`RuntimeError` otherwise, with the exit status and the ends
    of both output streams.
    """
    result = await commands.run(command, timeout=None)
    markers = [line for line in result.output.splitlines() if marker_re.fullmatch(line)]
    if result.exit_code != 0 or len(markers) != 1:
        raise RuntimeError(
            "native state save did not confirm its snapshot: "
            f"exit_code={result.exit_code}, stdout={result.stdout[-1000:]!r}, "
            f"stderr={result.stderr[-2000:]!r}"
        )


async def restart_resident_program(
    commands: SandboxCommands,
    *,
    program: str,
    lock: str,
    marker: str,
    label: str,
    error_code: str,
) -> None:
    """Restart supervisord's ``program`` for a changed profile, unless the box is pausing.

    The restart runs while holding ``lock``, the lock
    :func:`close_supervised_restarts` writes ``marker`` under, so a restart
    and a pause never interleave, and a restart that finds ``marker`` does not
    run. The command has no deadline: the program may take supervisor's stop
    wait before it starts again.

    Raises ``ASSISTANT_WORKSPACE_NOT_READY`` (409) when the box is pausing, and
    ``error_code`` (502), with the command's output, when supervisor does not
    report the program started. ``label`` names the program in both messages,
    for example ``"Hermes gateway"``. Raises :class:`ValueError` when ``lock``
    and ``marker`` are not in one directory.
    """
    directory = _supervise_directory(lock, marker)
    restart = (
        f"test ! -e {shlex.quote(marker)} || exit {_RESTART_REFUSED_EXIT}; "
        f"exec supervisorctl restart {shlex.quote(program)}"
    )
    result = await commands.run(
        "bash -lc "
        + shlex.quote(
            "set -euo pipefail; mkdir -p "
            + shlex.quote(directory)
            + "; exec flock " + shlex.quote(lock)
            + " bash -c " + shlex.quote(restart)
        ),
        timeout=None,
    )
    if result.exit_code == _RESTART_REFUSED_EXIT:
        raise APIError(
            code="ASSISTANT_WORKSPACE_NOT_READY",
            message=(
                f"the Assistant is pausing, so its {label} is not "
                "restarted for a changed profile"
            ),
            status_code=409,
        )
    output = result.output
    if result.exit_code != 0 or "started" not in output:
        failure = f"exit_code={result.exit_code}" if result.exit_code != 0 else "no start marker"
        raise APIError(
            code=error_code,
            message=(
                f"the {label} did not restart for a changed profile, so it "
                f"would keep serving the old one: {failure}; "
                f"output={output[:2000]!r}"
            ),
            status_code=502,
        )


async def wait_until_answering(
    commands: SandboxCommands,
    probe: str,
    budget_s: float,
    *,
    waiting_statuses: tuple[str, ...],
    foreign: Callable[[str], RuntimeProtocolError],
    unready: Callable[[int], APIError],
) -> None:
    """Wait, inside the box, until ``probe`` reports the runtime ready.

    ``probe`` is one shell command that prints the HTTP status the runtime's
    readiness check answered, or nothing or ``000`` while nothing answers; its
    standard error is discarded. The box runs it again and again, half a
    second apart, until ``budget_s``, rounded up to whole seconds, has passed.
    Each round starts a subshell, the probe and ``sleep`` on the CPU the
    runtime is starting on, so a shorter interval delays the very start it
    waits for.
    ``200`` is ready; no answer yet, or one of ``waiting_statuses`` (the
    runtime's own not-ready answers), keeps the wait going. Any other output
    means a program that is not the runtime serves what the probe asks, which
    waiting cannot change.

    Raises ``foreign(answer)`` at once on such an output, ``answer`` being the
    last line the probe printed, and ``unready(exit_code)`` when the wait ends
    any other way short of ready; exit code 124 is the budget spent. The
    host's own deadline is the budget plus 30 seconds, past which the command
    face raises :class:`TimeoutError`.
    """
    waiting = "|".join((*waiting_statuses, "000", '""'))
    loop = (
        # bash's SECONDS counts whole seconds.
        f"deadline=$((SECONDS + {math.ceil(budget_s)})); "
        "while :; do "
        f"code=$({probe} 2>/dev/null); "
        f'case "$code" in 200) exit 0;; {waiting}) ;; '
        f'*) echo "$code"; exit {_FOREIGN_ANSWER_EXIT};; esac; '
        'if [ "$SECONDS" -ge "$deadline" ]; then exit 124; fi; sleep 0.5; done'
    )
    result = await commands.run("bash -c " + shlex.quote(loop), timeout=budget_s + 30)
    if result.exit_code == _FOREIGN_ANSWER_EXIT:
        lines = (result.stdout or "").strip().splitlines()
        raise foreign(lines[-1] if lines else "an unknown status")
    if result.exit_code != 0:
        raise unready(result.exit_code)
