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

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    release_session,
    create_session,
    poll_until_agent_ready,
)

pytestmark = pytest.mark.e2e


def _run_terminal(client: httpx.Client, sid: str, command: str) -> tuple[str, str, int | None, list[dict]]:
    """Run one command via ``terminal/stream`` SSE; return (stdout, stderr, exit_code, events)."""
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
