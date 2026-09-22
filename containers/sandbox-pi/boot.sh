#!/bin/sh
# The pi box's boot contract: render pi's provider configuration, then become
# the base image's init.
#
# The renderer runs from the image because not every box is constructed by the
# host that later claims it:
#
#   * a POOLED box is created from a template before any session exists, and
#     the lifecycle server adds nothing to it;
#   * a RESUMED box is a fresh boot on committed files — the files return, the
#     processes do not.
#
# Both need pi's models.json present before the first conversation, and neither
# offers a channel to build it through at claim time. Its inputs are
# deployment-level (the gateway base URL and model), so they are already in the
# environment when the container starts.
#
# The renderer is backgrounded and the base entrypoint is `exec`'d so it stays
# PID 1's real process: it is the AIO base's init and reaps zombies, which a
# lingering shell parent would take over. The renderer waits for the account
# that init creates.
set -e

: "${ASTRABOX_PI_RENDERER:=/opt/astrabox/render-models-config.sh}"
: "${ASTRABOX_PI_RENDER_LOG:=/tmp/astrabox-pi-config.log}"

if [ ! -x "$ASTRABOX_PI_RENDERER" ]; then
    # Fail loud rather than boot a box that can never hold a conversation: pi
    # reaches its model gateway only through the file this renderer writes.
    echo "astrabox pi boot: renderer missing at $ASTRABOX_PI_RENDERER" >&2
    exit 1
fi

"$ASTRABOX_PI_RENDERER" >>"$ASTRABOX_PI_RENDER_LOG" 2>&1 &

exec /opt/gem/run.sh "$@"
