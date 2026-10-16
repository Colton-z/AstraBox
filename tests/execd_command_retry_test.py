"""A connection to execd that drops before the exit status arrives.

A command declared idempotent runs again; any other reports the failure,
since the box may already have run it.
"""

from __future__ import annotations

import httpx
import pytest

from astrabox.providers.execd import ExecdCommands
from astrabox.seams.sandbox import SandboxUnreachable
from tests._execd_fakes import ExecdRun, ScriptedExecdCommands

_DROP = httpx.RemoteProtocolError("peer closed connection without sending complete message body")


async def test_an_idempotent_command_runs_again_after_a_dropped_connection() -> None:
    execd = ScriptedExecdCommands(_DROP, ExecdRun(stdout=["ready"]))

    result = await ExecdCommands.over(execd, sandbox_id="box").run("probe", idempotent=True)

    assert (result.exit_code, result.stdout) == (0, "ready")
    assert execd.commands == ["probe", "probe"]


async def test_another_command_reports_a_dropped_connection_without_running_again() -> None:
    execd = ScriptedExecdCommands(_DROP, ExecdRun())

    with pytest.raises(SandboxUnreachable):
        await ExecdCommands.over(execd, sandbox_id="box").run("write")

    assert execd.commands == ["write"]
