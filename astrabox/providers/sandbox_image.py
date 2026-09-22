"""Shared agent-image contract — the single point every sandbox backend reads.

The community agent image (``containers/sandbox-claude-code/Dockerfile``, built on
the AIO base image) is a CONTRACT, not one backend's implementation detail: any
backend that boots this image relies on the same baked facts. Each constant below
mirrors exactly one line of that image; change the image and these constants
together (image-is-the-contract, the OpenHands ``get_agent_server_image`` idiom).

The constants live here — a leaf module with no provider imports — so no backend
has to import another backend's executor just to know what the image bakes.
"""

from __future__ import annotations

import os

from astrabox.config.release_images import release_image

# Agent container image. The image pre-bakes ``@anthropic-ai/claude-code`` so the
# CLI is on PATH (image-is-the-contract). ``ASTRABOX_AGENT_IMAGE`` names it per
# deployment; unset, the deployment runs its own release's published image
# (:func:`astrabox.config.release_images.release_image`).
# OpenSandbox reaches file and command operations through execd's native APIs.
# AIO remains the bundled image's process/account lifecycle base; it is not the
# file or command transport.
DEFAULT_AGENT_IMAGE_ENV = "ASTRABOX_AGENT_IMAGE"
AGENT_IMAGE_COMPONENT = "sandbox-claude-code"

# The boot environment every AstraBox sandbox carries, whatever engine runs in
# it and whichever rung of the ladder produced the box. ``IS_SANDBOX`` is what
# makes a box self-describing in a diagnostic report — it is the deployment fact
# a reader needs before any session fact means anything — and ``DISABLE_BROWSER``
# keeps the AIO base image from starting a browser no agent loop uses.
#
# Composed by the platform into the create spec rather than added by a provider,
# because the seam defines ``env`` as the boot environment written verbatim: a
# provider that injected its own would make the neutral spec a lie, and one that
# did while another did not is how boxes of different engines came to disagree
# on what an AstraBox sandbox looks like.
# AIO initializes browser directories even when its browser is disabled. Its
# supported download override keeps that unused scaffold out of user projects.
SANDBOX_SELF_DESCRIPTION = {
    "IS_SANDBOX": "1",
    "DISABLE_BROWSER": "true",
    "BROWSER_DOWNLOAD_DIR": "/tmp/astrabox-browser-downloads",
}

# The in-box CLI binary name: the image pre-bakes it so ``claude`` resolves on
# PATH; an executor may offer an install fallback only when it is genuinely
# absent.
IN_BOX_CLI = "claude"

# In-box path of the baked runner (the Dockerfile COPYs it here; the image's
# boot script starts it). Read by the executor's revive path — the one case
# where the host starts it is a runner that answered once and then died.
IN_BOX_RUNNER = "/opt/astrabox/sandbox_runner.py"

# Image-owned runner launcher. Both boot and backend recovery invoke this one
# path so the resident runner and every CLI child share the image workload uid.
IN_BOX_RUNNER_LAUNCHER = "/opt/astrabox/start-runner.sh"

# The fixed in-box port the runner binds (boot.sh exports this as
# ASTRABOX_RUNNER_PORT). It listens on 0.0.0.0, so a backend that can reach
# the box over TCP addresses this port directly; one that cannot reaches it
# through a published host mapping instead.
IN_BOX_SIDECAR_PORT = 8000

# The agent image's boot contract: start the resident control server, then exec
# the AIO base's own entrypoint (``/opt/gem/run.sh``), which owns workload-account
# creation and the base process lifecycle.
#
# The control server is started by the IMAGE rather than by the host reaching in,
# because a box can exist without the host having built it — a pooled box comes
# from a template, and a resumed box is a fresh boot on committed files — and a
# server the host has to come along and launch is missing on exactly those. A
# backend whose create API substitutes its own default entrypoint (the OpenSandbox
# SDK swaps in ``tail -f /dev/null`` even for an explicit empty list) must pass
# this value explicitly. Change it and ``containers/sandbox-claude-code/boot.sh``
# together — that file IS this contract.
AIO_IMAGE_ENTRYPOINT = ("/opt/astrabox/boot.sh",)


def resolve_agent_image() -> str:
    """The effective agent image: ``$ASTRABOX_AGENT_IMAGE``, else the release image."""
    return str(
        os.environ.get(DEFAULT_AGENT_IMAGE_ENV) or release_image(AGENT_IMAGE_COMPONENT)
    )


__all__ = [
    "AGENT_IMAGE_COMPONENT",
    "AIO_IMAGE_ENTRYPOINT",
    "DEFAULT_AGENT_IMAGE_ENV",
    "IN_BOX_CLI",
    "IN_BOX_RUNNER",
    "IN_BOX_RUNNER_LAUNCHER",
    "IN_BOX_SIDECAR_PORT",
    "resolve_agent_image",
]
