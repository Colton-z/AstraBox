"""OpenSandbox provider — sandbox lifecycle over a remote OpenSandbox server.

Architectural position
----------------------
The built-in sandbox backend, registered under the name ``open_sandbox`` and the
default this deployment resolves. The host creates sandboxes through the
OpenSandbox lifecycle API (the public SDK: ``Sandbox`` / ``SandboxManager``).
The box's resident control server owns the engine transport, and sandboxes that
use protected credentials also run the OpenSandbox egress sidecar. All backend
behaviour is encapsulated in this package; the engine and the seams are
unchanged.

Non-durable: the platform owns sandbox creation and the in-box filesystem is
ephemeral — it lives only inside the box and is lost when its TTL reaps it.

Deployment prerequisites (fail-loud, never silently degraded)
-------------------------------------------------------------
* ``ASTRABOX_SANDBOX_OPENAPI_BASE_URL`` must carry an explicit ``http://`` or
  ``https://`` scheme. Configuration resolution refuses an implicit scheme
  rather than choosing a transport on the operator's behalf (``_config.py``).
* The host must be able to reach the box's in-box port 8000 over TCP (the
  resident control server binds ``0.0.0.0:8000``): the direct WebSocket turn
  path and the data-plane HTTP requests both use it.
* ``ASTRABOX_SIDECAR_REVISION`` must NOT be configured for this backend. The
  executor reports an empty sidecar-generation owner (the unfenced posture);
  with a revision expected, the engine's "revision set but generation missing"
  guard fails the runtime bind with a 502. The env belongs to generation-fenced
  backends only.
* Both built-in engines acquire their sandboxes through this provider seam.
"""
