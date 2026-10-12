"""A stand-in for the OpenSandbox SDK's execd command client.

It reports each command the way the SDK does: every event goes through the
SDK's own dispatcher to the handlers the caller passed, so a test of code built
on :class:`astrabox.providers.execd.ExecdCommands` sees what that code sees in a
box.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from opensandbox.adapters.converter.event_node import EventNode
from opensandbox.adapters.converter.execution_event_dispatcher import (
    ExecutionEventDispatcher,
)
from opensandbox.models.execd import Execution


@dataclass
class ExecdRun:
    """What execd reports for one command: its output lines and how it ended.

    A zero ``exit_code`` ends with the completion event and any other value
    with the error event execd sends for a non-zero exit; ``None`` sends
    neither. ``errors`` are further error events, after the exit status.
    """

    stdout: Sequence[str] = ()
    stderr: Sequence[str] = ()
    exit_code: int | None = 0
    errors: Sequence[tuple[str, str]] = field(default_factory=tuple)


async def report(run: ExecdRun, handlers: Any) -> Execution:
    """Deliver ``run``'s events to ``handlers`` through the SDK's dispatcher."""

    execution = Execution(id=None, execution_count=None, result=[], error=None)
    dispatcher = ExecutionEventDispatcher(execution, handlers)
    for text in run.stdout:
        await dispatcher.dispatch(EventNode(type="stdout", text=text, timestamp=0))
    for text in run.stderr:
        await dispatcher.dispatch(EventNode(type="stderr", text=text, timestamp=0))
    if run.exit_code is not None:
        if run.exit_code == 0:
            await dispatcher.dispatch(
                EventNode(type="execution_complete", timestamp=0, execution_time=1)
            )
        else:
            await dispatcher.dispatch(
                EventNode(
                    type="error",
                    timestamp=0,
                    error={"ename": "CommandExecError", "evalue": str(run.exit_code)},
                )
            )
    for name, value in run.errors:
        await dispatcher.dispatch(
            EventNode(type="error", timestamp=0, error={"ename": name, "evalue": value})
        )
    return execution


class ScriptedExecdCommands:
    """The SDK's ``Commands`` face, answering from a script.

    Each command takes the next outcome: an :class:`ExecdRun` to report, or an
    exception to raise. With the script used up, a command succeeds silently.
    """

    def __init__(self, *outcomes: ExecdRun | BaseException) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._outcomes = list(outcomes)

    async def run(self, command: str, *, opts: Any = None, handlers: Any = None) -> Execution:
        self.calls.append((command, opts))
        outcome = self._outcomes.pop(0) if self._outcomes else ExecdRun()
        if isinstance(outcome, BaseException):
            raise outcome
        return await report(outcome, handlers)

    @property
    def commands(self) -> list[str]:
        return [command for command, _opts in self.calls]
