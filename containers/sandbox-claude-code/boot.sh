#!/bin/sh
# The agent box's boot contract: bring up the in-box runner, then become the
# base image's init.
#
# The resident runner (`sandbox_runner.py`) owns the Claude Agent SDK session
# and speaks the envelope protocol to the host. It must start from the image,
# because not every box is constructed by the host that later adopts it:
#
#   * a POOLED box, created from a template before any session claims it. The
#     runner must already be present when the session claims it.
#   * a RESUMED box. Pause commits the filesystem and frees the compute, so a
#     resume is a fresh boot on committed files: the files return but processes
#     do not. Without image-owned startup, `:8000` refuses connections and the
#     first turn after waking fails with `runtime reconnect failed`.
#
# Both cases require the same contract: whatever the platform needs a box to
# have belongs in the image. The image starts its own server, and every provenance —
# cold-created, pooled, resumed — comes up identically.
#
# The host executor's launch is idempotent (it adopts a listening runner), so
# a host-driven exec launch and this boot cannot double-start the process.
#
# The runner is backgrounded and the base entrypoint is `exec`'d so it stays
# PID 1's real process: it is the AIO base's init and reaps zombies, which a
# lingering shell parent would take over.
set -e

: "${ASTRABOX_INBOX_SERVER:=/opt/astrabox/sandbox_runner.py}"
: "${ASTRABOX_INBOX_LOG:=/tmp/astrabox_runner.log}"
: "${ASTRABOX_RUNNER_LAUNCHER:=/opt/astrabox/start-runner.sh}"
: "${ASTRABOX_RUNNER_PORT:=8000}"
export ASTRABOX_RUNNER_PORT

if [ ! -f "$ASTRABOX_INBOX_SERVER" ]; then
    # Fail loud rather than boot a box that can never serve a turn: a missing
    # server means the image is not the one this platform contracts for.
    echo "astrabox boot: runner missing at $ASTRABOX_INBOX_SERVER" >&2
    exit 1
fi
if [ ! -x "$ASTRABOX_RUNNER_LAUNCHER" ]; then
    echo "astrabox boot: runner launcher missing at $ASTRABOX_RUNNER_LAUNCHER" >&2
    exit 1
fi

"$ASTRABOX_RUNNER_LAUNCHER" >>"$ASTRABOX_INBOX_LOG" 2>&1 &

exec /opt/gem/run.sh "$@"
