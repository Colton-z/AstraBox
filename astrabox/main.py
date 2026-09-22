"""Application entry point.

Boots a plain FastAPI app under uvicorn:

* :data:`app` — a ready ASGI application built by
  :func:`astrabox.api.app.create_app`. Its ``lifespan`` already owns graceful
  shutdown: on stop it runs ``close_services_for_lifecycle()`` to release
  resources. So ``uvicorn astrabox.main:app`` gets startup/shutdown wiring for
  free.
* :func:`run` — a thin programmatic entry (the ``astrabox.main:run`` script target
  in ``pyproject``) that hands :data:`app`'s factory to ``uvicorn.run``. The
  ``astrabox`` console script in ``[project.scripts]`` points at
  :func:`astrabox.cli.main`'s ``serve`` verb, which boots the *same* factory; this
  ``run`` is the no-argparse equivalent for programmatic/container use.

Lifecycle is the FastAPI ``lifespan``, identity is the no-auth default, secrets are
env vars, config is YAML/env. This module only builds the app and (in ``run``)
starts the server.
"""

from __future__ import annotations

import os

from astrabox.api.app import create_app

#: uvicorn factory import string for :data:`app`'s factory. Matches
#: ``astrabox.cli.APP_FACTORY`` so the CLI ``serve`` verb and this ``run`` boot the
#: exact same application factory.
APP_FACTORY = "astrabox.api.app:create_app"

#: The process ASGI application. Import target for ``uvicorn astrabox.main:app``
#: and for any embedding host. Built once at import via the shared factory, so it
#: carries the same ``lifespan`` (graceful shutdown → ``close_services_for_lifecycle``)
#: as the CLI path.
app = create_app()

# Loopback by default (no API auth + root-equivalent Docker socket). Set
# ASTRABOX_HOST=0.0.0.0 explicitly to bind all interfaces.
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def run() -> None:
    """Boot the AstraBox API server under uvicorn (programmatic entry).

    Bind address/port default from ``ASTRABOX_HOST`` / ``ASTRABOX_PORT``, which
    the server image sets in its ENV, so ``docker run`` needs no flags;
    precedence is env var > literal default (``127.0.0.1:8000``). uvicorn is
    given the app factory import string (``factory=True``) rather than the
    prebuilt :data:`app`, so uvicorn owns worker/process lifecycle consistently
    with the CLI's ``serve`` path. A malformed ``ASTRABOX_PORT`` fails loud via
    ``int`` parsing.
    """
    import uvicorn

    host = os.environ.get("ASTRABOX_HOST", _DEFAULT_HOST)
    port = int(os.environ.get("ASTRABOX_PORT", _DEFAULT_PORT))
    log_level = os.environ.get("ASTRABOX_LOG_LEVEL", "info")
    uvicorn.run(
        APP_FACTORY,
        host=host,
        port=port,
        log_level=log_level,
        factory=True,
    )


if __name__ == "__main__":
    run()
