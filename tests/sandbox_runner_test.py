"""The in-box runner fails closed and leaves an account of a Claude CLI that dies.

The permission gate denies when it errors. A CLI process that exits leaves
operators a bounded, redacted account of it on the failed response, while the
conversation is told only the exit code.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astrabox.core.service.orchestrator import sandbox_runner
from astrabox.core.service.orchestrator.sandbox_runner import (
    DeliveryCommand,
    InteractionBroker,
)
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.response_folds import ResponseFolds
from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from tests._claude_session import accept, create_session, record


# --- event pump -------------------------------------------------------------


async def test_a_gate_that_errors_denies_instead_of_leaking() -> None:
    """Fail CLOSED: an exception inside the gate must become a deny decision.

    Under the CLI's bypass base mode an errored hook falls through to "run
    it", so an exception escaping the gate executes an unapproved tool. The
    belt converts any unexpected error into the one answer that cannot be
    wrong.
    """

    class _ExplodingSender:
        host_connected = True

        async def send(self, *args, **kwargs):
            raise RuntimeError("wire fell over")

    broker = InteractionBroker(_ExplodingSender(), wait_budget_s=5.0)  # type: ignore[arg-type]
    out = await broker.pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "x"}}, "t1", None
    )

    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert "permission gate error" in hook["permissionDecisionReason"]


# --- CLI process exit -------------------------------------------------------

#: The model credential the host hands the CLI in ``prepare.options.env``. It
#: has no recognisable shape, so only redaction by value can remove it.
_GATEWAY_KEY = "gw-virtual-key-5f0c2d9e7a41"
#: A login the CLI read from its own files, which the runner never held.
_CLI_LOGIN = "sk-ant-oat01-" + "Q" * 40
_LAST_LINE = "FATAL: sentinel-7731 the CLI's last words"

#: A stand-in for ``/usr/local/bin/claude`` that speaks the SDK's stdio
#: protocol: it answers ``initialize``, echoes the prompt it takes the way the
#: CLI replays a user message, writes ``STDERR`` and exits with status 137.
_FAKE_CLI = """#!{python}
import json, os, sys
STDERR = {stderr}
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("type") == "control_request":
        print(json.dumps({{"type": "control_response", "response": {{
            "subtype": "success", "request_id": message["request_id"], "response": {{}}}}}}), flush=True)
    elif message.get("type") == "user":
        print(json.dumps(message), flush=True)
        for line in STDERR:
            sys.stderr.write(line.replace("@AUTH@", os.environ["ANTHROPIC_AUTH_TOKEN"]) + "\\n")
        sys.stderr.flush()
        sys.exit(137)
"""


class _Sink:
    """The runner's host link: keeps every frame it sends."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


async def _run_cli_until_it_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, session_id: str, input_id: str, stderr: list[str]
) -> list[dict[str, Any]]:
    """The frames a real runner session sends while the stand-in CLI takes one prompt and exits."""
    cli = tmp_path / "claude"
    cli.write_text(_FAKE_CLI.format(python=sys.executable, stderr=repr(stderr)), encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setattr(sandbox_runner, "CLAUDE_CLI_PATH", str(cli))
    monkeypatch.setenv("ASTRABOX_RUNNER_SPOOL_DIR", str(tmp_path / "spool"))
    sink = _Sink()
    session = sandbox_runner._real_session_factory(
        {"slot_id": "slot-1", "options": {"env": {"ANTHROPIC_AUTH_TOKEN": _GATEWAY_KEY}}}, sink
    )
    await session.prepare()
    await session.activate(session_id, sink, store={"base_url": "http://127.0.0.1:9"})
    await session.sender.replay_after(0)
    try:
        await session.submit(DeliveryCommand(
            command_id=input_id,
            session_id=session_id,
            sequence=1,
            sdk_input={
                "type": "user",
                "message": {"role": "user", "content": "Summarise the build log"},
                "parent_tool_use_id": None,
                "session_id": session_id,
                "uuid": input_id,
            },
        ))
        async with asyncio.timeout(20):
            while not any(frame["op"] == "error" for frame in sink.frames):
                await asyncio.sleep(0.05)
    finally:
        await session.stop()
    return sink.frames


def _filler(count: int) -> list[str]:
    return [f"at step {index:04d} ({'.' * 60})" for index in range(count)]


@pytest.mark.usefixtures("database")
@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(
            [*_filler(200), "retrying with @AUTH@ after 401", f"loaded login {_CLI_LOGIN}", _LAST_LINE],
            id="with-stderr",
        ),
        pytest.param([], id="without-stderr"),
    ],
)
async def test_a_cli_that_exits_leaves_operators_its_redacted_stderr_and_the_person_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: list[str]
) -> None:
    # A prompt is taken and the CLI exits 137 without a result. The failed
    # response must keep what the process said for an operator after its box
    # is gone, name the box, keep no credential, and never let the
    # conversation read the stderr. Exit status 137 is a number the process
    # exited with: nothing may call it out-of-memory.
    session_id = await create_session()
    input_id = await accept(session_id, "Summarise the build log")
    frames = await _run_cli_until_it_exits(
        tmp_path, monkeypatch, session_id=session_id, input_id=input_id, stderr=stderr
    )
    await record(session_id, cast(Any, SimpleNamespace(frames=frames)), sandbox_id="sandbox-7")

    facts = SessionRuntimeRepository()
    response = await facts.response_for_input(session_id, input_id)
    assert response is not None
    assert (response["outcome"], response["native_reason"]) == ("failed", "ProcessError")
    assert response["error"] == {"code": "ProcessError", "message": "Claude Code exited with code 137."}

    diagnostic = dict(response["diagnostic"])
    if stderr:
        tail = diagnostic.pop("stderr_tail")
        assert tail.endswith(_LAST_LINE)
        assert len(tail) <= sandbox_runner.CLI_STDERR_TAIL_CHARS
        assert "at step 0000" not in tail, "the bound keeps the newest lines"
        assert f"retrying with [redacted] after 401\nloaded login [redacted]\n{_LAST_LINE}" in tail
        assert _GATEWAY_KEY not in tail and _CLI_LOGIN not in tail
        assert diagnostic.pop("stderr_omitted_chars") > 0
        assert diagnostic == {
            "exit_code": 137, "terminal_reason": None, "stderr": "captured", "sandbox_id": "sandbox-7",
        }
    else:
        assert diagnostic == {
            "exit_code": 137, "terminal_reason": None, "stderr": "empty", "sandbox_id": "sandbox-7",
        }
    assert await facts.response_diagnostics(session_id, [response["response_id"]]) == {
        response["response_id"]: response["diagnostic"]
    }

    folds = ResponseFolds(facts=facts, reads=SessionReadRepository())
    runtime = SessionRuntimeService(
        sessions=SessionRepository(), repo=facts, feed=SessionFeed(), host=cast(Any, None), folds=folds
    )
    page = json.dumps(await runtime.history_for(session_id))
    assert "Claude Code exited with code 137." in page
    assert "sentinel-7731" not in page and "stderr" not in page.lower()

    record_text = json.dumps({"error": response["error"], "diagnostic": response["diagnostic"]}).lower()
    assert "oom" not in record_text and "memory" not in record_text
