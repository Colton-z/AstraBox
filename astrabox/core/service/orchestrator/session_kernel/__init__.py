"""Session Kernel package.

Intentionally THIN: importing this package must not drag the heavy service/engine
layer. Import concrete submodules directly, e.g.::

    from astrabox.core.service.orchestrator.session_kernel.service import SessionKernelService

An eager ``from .service import SessionKernelService`` here would drag
service -> workers -> assistant -> engine -> turn_service into any kernel
submodule import, creating import-order-dependent cycles with
``config_resolver`` / ``conversation_identity``.
"""

from __future__ import annotations
