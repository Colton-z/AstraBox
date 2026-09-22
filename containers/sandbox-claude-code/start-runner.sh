#!/bin/sh
# Start the resident runner under the image-declared workload account.
set -eu

: "${ASTRABOX_INBOX_SERVER:=/opt/astrabox/sandbox_runner.py}"
: "${ASTRABOX_RUNNER_PORT:=8000}"
: "${ASTRABOX_WORKLOAD_USER:?ASTRABOX_WORKLOAD_USER is required}"
: "${WORKSPACE:?WORKSPACE is required}"

if [ ! -f "$ASTRABOX_INBOX_SERVER" ]; then
    echo "astrabox runner: missing server at $ASTRABOX_INBOX_SERVER" >&2
    exit 1
fi

# The AIO base creates its workload account during boot. The image starts this
# launcher concurrently so the base can remain PID 1; wait only for that fixed
# image capability, never create or alter an account here.
account_waits=0
while ! passwd_entry="$(getent passwd "$ASTRABOX_WORKLOAD_USER")"; do
    account_waits=$((account_waits + 1))
    if [ "$account_waits" -ge 300 ]; then
        echo "astrabox runner: workload account $ASTRABOX_WORKLOAD_USER was not created" >&2
        exit 1
    fi
    sleep 0.1
done

workload_home="$(printf '%s\n' "$passwd_entry" | cut -d: -f6)"
if [ -z "$workload_home" ]; then
    echo "astrabox runner: workload account $ASTRABOX_WORKLOAD_USER has no home" >&2
    exit 1
fi
if [ ! -d "$WORKSPACE" ]; then
    echo "astrabox runner: workspace missing at $WORKSPACE" >&2
    exit 1
fi

cd "$WORKSPACE"
exec runuser -u "$ASTRABOX_WORKLOAD_USER" -- env \
    HOME="$workload_home" \
    USER="$ASTRABOX_WORKLOAD_USER" \
    LOGNAME="$ASTRABOX_WORKLOAD_USER" \
    PWD="$WORKSPACE" \
    ASTRABOX_RUNNER_PORT="$ASTRABOX_RUNNER_PORT" \
    /usr/local/bin/python3.12 "$ASTRABOX_INBOX_SERVER"
