"""Deployment entry points: the processes a packaged AstraBox install boots.

Two modules, both runnable with ``python -m``:

* :mod:`astrabox.deploy.sandbox_server` — starts the OpenSandbox lifecycle API
  (``opensandbox-server``) on loopback, configured entirely from ``ASTRABOX_*``.
* :mod:`astrabox.deploy.onebox` — the container entry point: decides whether
  that server is needed, supervises it and the AstraBox API server together, and
  makes either one dying take the whole container down.

Nothing is imported here. ``sandbox_server`` needs the ``sandbox-server``
optional extra, so importing it eagerly would break the clean-boot invariant
(``import astrabox`` must never depend on an extra); ``onebox`` pulls in the
subprocess/signal machinery no library caller wants. Import the module you mean.
"""

from __future__ import annotations

__all__: list[str] = []
