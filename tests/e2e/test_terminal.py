"""Terminal and shell-exec e2e for stdout and exit-code propagation.

The Terminal panel runs one command per request over HTTP SSE (no WebSocket on the
Community HTTP-only path). The selected sandbox backend dispatches the command to
the in-box shell, and the result's real ``exit_code`` is carried back verbatim.

This pins the **faked-success** signature: a non-zero exit being reported as
``0`` (the upstream SDK's ``Execution.exit_code or 0`` idiom). So the
load-bearing assertion is that ``exit 7`` surfaces ``exit_code == 7`` — not 0 —
alongside the command's stdout. A zero-exit command and a stderr+non-zero command
are checked too, so a backend that hard-codes either polarity fails.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_terminal.py -m e2e -s
"""

from __future__ import annotations

import json
import shlex
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_agent_variant_session,
    data,
    environment_with_tenancy,
    release_session,
    create_session,
    poll_until_agent_ready,
)

pytestmark = pytest.mark.e2e


def _run_terminal(
    client: httpx.Client,
    sid: str,
    command: str,
    on_event: Callable[[dict], None] | None = None,
) -> tuple[str, str, int | None, list[dict]]:
    """Run one command via ``terminal/stream`` SSE; return (stdout, stderr, exit_code, events).

    ``on_event`` sees each event as it arrives, for a caller that acts while the
    command is still running.
    """
    stdout: list[str] = []
    stderr: list[str] = []
    exit_code: int | None = None
    events: list[dict] = []
    with client.stream(
        "POST",
        f"/api/v1/sessions/{sid}/terminal/stream",
        json={"command": command},
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(90.0, connect=30.0),
    ) as resp:
        ctype = resp.headers.get("content-type", "")
        assert "text/event-stream" in ctype, (
            f"terminal did not return SSE (content-type={ctype!r}); body={resp.read()[:400]!r}"
        )
        for raw in resp.iter_lines():
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            events.append(ev)
            if on_event is not None:
                on_event(ev)
            etype = ev.get("type")
            if etype == "stdout":
                stdout.append(str(ev.get("text") or ""))
            elif etype == "stderr":
                stderr.append(str(ev.get("text") or ""))
            elif etype == "exit":
                raw_code = ev.get("exit_code")
                exit_code = int(raw_code) if raw_code is not None else None
    return "".join(stdout), "".join(stderr), exit_code, events


def test_terminal_nonzero_exit_propagates(e2e_client: httpx.Client) -> None:
    """A command's stdout AND its true non-zero exit code both propagate (not faked 0)."""
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        # Non-zero exit: exit 7 must NOT come back as 0.
        # `bash -c` so the non-zero exit ends a CHILD, not the session shell —
        # a top-level `exit` kills bash itself, which is a different behaviour
        # with its own test below (and an upstream output-loss race).
        out, _err, code, events = _run_terminal(
            e2e_client, sid, "bash -c 'echo HELLO_STDOUT; exit 7'"
        )
        assert "HELLO_STDOUT" in out, f"stdout not propagated: out={out!r} events={events}"
        assert code == 7, (
            f"non-zero exit faked/lost: got exit_code={code!r} (expected 7); events={events}"
        )

        # Zero exit: the success polarity still works (a backend hard-coding 1 fails here).
        out0, _e0, code0, _ev0 = _run_terminal(e2e_client, sid, "echo OK_ZERO")
        assert "OK_ZERO" in out0, f"zero-exit stdout not propagated: {out0!r}"
        assert code0 == 0, f"zero-exit command reported non-zero: exit_code={code0!r}"

        # stderr + non-zero: the failed-command shape (stderr surfaced, exit carried).
        out3, err3, code3, ev3 = _run_terminal(
            e2e_client, sid, "bash -c 'echo OUT3; echo ERR3 1>&2; exit 3'"
        )
        assert "OUT3" in out3, f"stdout missing on a failing command: {out3!r}"
        assert code3 == 3, f"non-zero exit lost on a stderr command: exit_code={code3!r} events={ev3}"
        # stderr can be folded by the exec stream; assert it if it is separate.
        if err3:
            assert "ERR3" in err3, f"stderr present but wrong: {err3!r}"

        # The shell-killing form: `exit` at the top level ends bash itself,
        # before the wrapper's status marker on the next line can run. Whether
        # anything else reports the code is the image's: measured in one lane,
        # the claude-code sandbox answers only `RuntimeError: read stdout: read
        # |0: file already closed` while the deepseek-harness sandbox answers
        # `exit_code: 9`. Its execd build differs, so this cannot assert either
        # one.
        #
        # What holds on both, and is what this file is for: the number a caller
        # sees is the process's own, or it is absent and said to be absent.
        # Never a substitute passed off as real.
        _outx, errx, codex, evx = _run_terminal(e2e_client, sid, "exit 9")
        if codex == 9:
            assert "did not report an exit status" not in errx, (
                f"the exit status arrived, so nothing should claim it did not; "
                f"stderr={errx!r} events={evx}"
            )
        else:
            assert "did not report an exit status" in errx, (
                f"no exit status was reported, so the terminal must say so "
                f"rather than pass {codex!r} off as the process's own; "
                f"stderr={errx!r} events={evx}"
            )

        # The next command must receive a working shell after the previous one
        # exits; one command cannot leave the terminal permanently unavailable.
        out5, _err5, code5, ev5 = _run_terminal(
            e2e_client, sid, "bash -c 'echo AFTER_EXIT; exit 5'"
        )
        assert "AFTER_EXIT" in out5, (
            f"the terminal did not recover from a shell that exited: "
            f"out={out5!r} events={ev5}"
        )
        assert code5 == 5, (
            f"exit code lost on the shell that replaced an exited one: "
            f"exit_code={code5!r} events={ev5}"
        )
    finally:
        release_session(sid)


def test_an_interrupt_stops_the_command_in_a_conversation_box_terminal(
    e2e_client: httpx.Client,
) -> None:
    """An interrupt ends the running command, and the terminal takes the next one.

    A conversation with its own box runs its terminal as one persistent execd
    shell, addressed by its PTY session id. The interrupt has to reach that
    session: execd's command interrupt knows only the commands execd started
    itself, so a PTY session id sent there is refused and the command goes on.

    The command announces itself before it sleeps, so the interrupt is sent to
    a command known to be running. That it stopped is checked three ways: the
    stream ends with the interrupt's status, the next command runs at once
    rather than after the sleep, and the file the command would have written
    once the sleep ended never appears.
    """
    created = create_agent_variant_session(
        e2e_client,
        name_suffix="terminal-interrupt",
        engine_options={},
        environment_name=environment_with_tenancy(e2e_client, "conversation"),
    )
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    poll_until_agent_ready(e2e_client, sid)

    marker = f"/tmp/astrabox-terminal-interrupt-{uuid.uuid4().hex}"
    sleep_seconds = 30
    command = f"echo INTERRUPT_ME_STARTED; sleep {sleep_seconds} && touch {shlex.quote(marker)}"
    announced = threading.Event()

    def _watch(event: dict) -> None:
        if event.get("type") == "stdout" and "INTERRUPT_ME_STARTED" in str(event.get("text") or ""):
            announced.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        started = time.monotonic()
        running = pool.submit(_run_terminal, e2e_client, sid, command, _watch)
        assert announced.wait(timeout=60), "the command never announced itself"
        result = data(e2e_client.post(f"/api/v1/sessions/{sid}/interrupt", timeout=60.0))
        assert result.get("status") == "completed", result
        _out, _err, code, events = running.result(timeout=60)
    assert code == 130, f"the interrupted command did not end with the interrupt's status: {events}"

    probe = f"test -e {shlex.quote(marker)} && echo SURVIVED || echo STOPPED"
    asked = time.monotonic()
    out, _err, code, events = _run_terminal(e2e_client, sid, probe)
    # Well under the 10 s a connection to execd costs when its close waits
    # for a TCP close execd never sends, as well as under the sleep.
    assert time.monotonic() - asked < 8, (
        f"the next command waited behind the interrupted one: events={events}"
    )
    assert (code, "STOPPED" in out) == (0, True), f"out={out!r} events={events}"

    # Past the end of the sleep, a command that was only detached from would
    # have written its file.
    time.sleep(max(0.0, started + sleep_seconds + 5 - time.monotonic()))
    out, _err, code, events = _run_terminal(e2e_client, sid, probe)
    assert (code, "STOPPED" in out) == (0, True), (
        f"the interrupted command kept running in the box: out={out!r} events={events}"
    )
