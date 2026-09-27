"""The credential that gates a sandbox's AIO :8080 services.

The agent-infra base image runs a web gateway on :8080 in front of a file and
shell API, a terminal, JupyterLab, a VNC desktop, code-server and an MCP hub.
Unset, it serves them to anyone who can reach the port; on Kubernetes every Pod
in the cluster can, so an unset key is cross-tenant code execution. The base
image gates all of them behind ``SANDBOX_API_KEY`` when it is set
(``X-AIO-API-Key`` / ``Authorization: Bearer`` / ``?api_key=``), which is the
vendor's own mechanism.

AstraBox sets it per box, derived from the deployment secret and the box's
assignment id through the platform's master-key derivation
(:func:`derive_platform_key`), so:

* it is unguessable without the deployment secret — a co-tenant that reaches the
  port cannot compute it;
* it is per box — the key baked into one box's environment does not open another
  box's :8080, so an agent that reads its own ``SANDBOX_API_KEY`` gains nothing
  against a sibling;
* the key stays inside the box's create environment; AstraBox never places it
  in a browser URL, and the gateway port cannot be exposed as a preview.

The assignment id is the subject because it is the one identity known before
create — a prewarmed box is created before any session claims it — so the key
can be baked into the create environment for cold and prewarmed boxes alike.
"""

from __future__ import annotations

#: The base image's own environment variable (the vendor defines this name).
#: Setting it turns on the gateway's auth for every :8080 service.
AIO_SANDBOX_API_KEY_ENV = "SANDBOX_API_KEY"

#: The port the AIO web gateway serves on. Exposed-port links refuse it;
#: a user's own dev server runs on another port.
AIO_HTTP_PORT = 8080

#: Domain separation for the derived key, distinct from every other platform
#: secret purpose.
_AIO_AUTH_DOMAIN = "astrabox-aio-sandbox-http"


def derive_aio_api_key(assignment_id: str) -> str:
    """The :8080 gateway key for the box created under ``assignment_id``.

    A pure function of the deployment secret and the assignment id, so the
    create path can reproduce it without storing it elsewhere. Raises when the
    assignment id is empty: a box with no durable identity must not be given
    a guessable or shared credential.
    """
    from astrabox.core.service.orchestrator.platform_secret import (
        derive_platform_key,
        platform_secret_root,
    )

    subject = str(assignment_id or "").strip()
    if not subject:
        raise ValueError("AIO :8080 api key requires a non-empty assignment id")
    return derive_platform_key(
        platform_secret_root(), domain=_AIO_AUTH_DOMAIN, subject=subject
    ).hex()
