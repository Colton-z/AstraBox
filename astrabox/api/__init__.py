"""AstraBox HTTP/SSE surface.

This package owns the FastAPI application together with the session, message,
and AI-SDK data-stream-over-SSE routes.

Public surface
--------------
* :func:`create_app` — the FastAPI application factory.

Only the factory is exported here so that ``import astrabox.api`` stays cheap and
side-effect free; the app is constructed explicitly by the CLI
(``astrabox.cli``) or by an ASGI server pointed at ``astrabox.api.app:create_app``.
"""

from __future__ import annotations

from astrabox.api.app import create_app

__all__ = ["create_app"]
