"""`astrabox serve` ends a requested stop that completed with status 0.

uvicorn re-raises the stop signal after its graceful shutdown; under Python's
default handler the process then dies by that signal, and a clean `docker stop`
left the server container `Exited (241)`. These run a real uvicorn in a child
process, because the re-raise happens in uvicorn's own signal handling and a
fake would not have it.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap

import pytest

_APP = textwrap.dedent(
    """
    import sys

    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                if {fail_startup!r}:
                    await send({{"type": "lifespan.startup.failed", "message": "boom"}})
                    return
                await send({{"type": "lifespan.startup.complete"}})
                print("READY", flush=True)
            elif message["type"] == "lifespan.shutdown":
                await send({{"type": "lifespan.shutdown.complete"}})
                print("SHUTDOWN-COMPLETE", flush=True)
                return

    kwargs = dict(host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    if {bare!r}:
        import uvicorn
        uvicorn.run(app, **kwargs)
    else:
        from astrabox.cli.serve import _serve_until_stopped
        sys.exit(_serve_until_stopped(app, **kwargs))
    """
)


def _serve(*, bare: bool = False, fail_startup: bool = False) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", _APP.format(bare=bare, fail_startup=fail_startup)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_after_ready(process: subprocess.Popen[str]) -> tuple[int, str]:
    assert process.stdout is not None
    lines = []
    for line in process.stdout:
        lines.append(line)
        if line.strip() == "READY":
            break
    else:  # pragma: no cover - the child died before serving
        process.wait(timeout=10)
        pytest.fail("server exited before it was ready:\n" + "".join(lines))
    process.send_signal(signal.SIGTERM)
    rest, _ = process.communicate(timeout=30)
    return process.returncode, "".join(lines) + rest


def test_a_completed_stop_exits_zero() -> None:
    code, output = _stop_after_ready(_serve())
    assert "SHUTDOWN-COMPLETE" in output
    assert code == 0, output


def test_bare_uvicorn_dies_by_the_signal_it_shut_down_on() -> None:
    # The vendor behaviour the helper exists for: without it, the graceful
    # shutdown completes and the process still ends by SIGTERM.
    code, output = _stop_after_ready(_serve(bare=True))
    assert "SHUTDOWN-COMPLETE" in output
    assert code == -signal.SIGTERM, output


def test_a_failed_startup_stays_non_zero() -> None:
    process = _serve(fail_startup=True)
    output, _ = process.communicate(timeout=30)
    assert process.returncode not in (0, None), output
