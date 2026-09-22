"""A failed bootstrap carries every diagnostic emitted on the command wire.

execd can stream ``CONVERSATION_BOOTSTRAP_MISSING_ENV`` and
``CONVERSATION_BOOTSTRAP_FAILED`` before its terminal ``CommandExecError``.
The platform combines streamed events with the final log accumulator so the
terminal error cannot hide earlier output.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    _command_output,
    _last_dispatch_branch,
    run_sandbox_command,
)


def _result(*, stdout: list[str], error: object | None) -> object:
    return SimpleNamespace(
        logs=SimpleNamespace(
            stdout=[SimpleNamespace(text=line) for line in stdout], stderr=[]
        ),
        error=error,
    )


def test_the_streamed_lines_are_reported_when_the_accumulator_is_empty() -> None:
    streamed = [
        "CONVERSATION_BOOTSTRAP_MISSING_ENV name=CONV_USER",
        "CONVERSATION_BOOTSTRAP_FAILED line=33 rc=42 cmd=[user=...]",
    ]

    output = _command_output(
        _result(stdout=[], error="CommandExecError value='42'"), streamed=streamed
    )

    assert "line=33 rc=42" in output
    assert "MISSING_ENV" in output


def test_an_accumulated_log_is_still_read_when_nothing_was_streamed() -> None:
    """The final accumulator remains authoritative without stream handlers."""
    output = _command_output(
        _result(stdout=["CONVERSATION_BOOTSTRAP_READY user=conv_x"], error=None)
    )

    assert "CONVERSATION_BOOTSTRAP_READY" in output


def test_nothing_at_all_says_so_rather_than_reading_as_silence() -> None:
    """The control: no output from either source is reported as no capture.

    Without it, an empty answer collapses into the error's own repr, which
    reads as "the script said nothing" and is indistinguishable from "nothing
    was captured" — the difference between a silent failure and a lost one.
    """
    output = _command_output(
        _result(stdout=[], error="CommandExecError value='1'"), streamed=[]
    )

    assert "<no stdout or stderr captured from the sandbox command>" in output
    assert "CommandExecError" in output


async def test_concurrent_commands_keep_their_own_dispatch_evidence() -> None:
    """A racing bootstrap cannot overwrite the branch another one reports."""

    opts_entered = asyncio.Event()
    envs_entered = asyncio.Event()

    async def via_opts(_command: str, *, opts: object) -> object:
        assert opts is not None
        opts_entered.set()
        await envs_entered.wait()
        return _result(stdout=[], error=None)

    async def via_envs(
        _command: str,
        *,
        envs: dict[str, str] | None = None,
        timeout_in_millis: int | None = None,
    ) -> object:
        assert envs == {"INPUT": "present"}
        assert timeout_in_millis == 100
        await opts_entered.wait()
        envs_entered.set()
        return _result(stdout=[], error=None)

    async def dispatch(run_fn: object) -> str:
        await run_sandbox_command(
            run_fn,
            "true",
            envs={"INPUT": "present"},
            timeout_in_millis=100,
        )
        return _last_dispatch_branch()

    opts_branch, envs_branch = await asyncio.gather(
        dispatch(via_opts),
        dispatch(via_envs),
    )

    assert opts_branch == "opts"
    assert envs_branch == "envs"
