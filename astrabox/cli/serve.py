"""The operator subcommands: ``astrabox serve`` and the deployment checks.

These run against the deployment from the inside — ``serve`` boots the FastAPI
application (:func:`astrabox.api.app.create_app`) under uvicorn on ``:8000`` by
default, and the verification commands exercise a live deployment through the
same configured provider path. The container image's ENTRYPOINT is ``astrabox``
and its CMD is ``serve``, so ``docker run <image>`` lands here.

The client subcommands that configure a *running* deployment over HTTP live in
:mod:`astrabox.cli.resources`; :func:`astrabox.cli.main` assembles both halves
into one parser.

Identity defaults to no-auth (``ASTRABOX_WEB_IDENTITY``) and secrets come from
env vars; the handler parses args and hands off to ``uvicorn.run``.

Design notes
------------
* ``--host``/``--port`` default from ``ASTRABOX_HOST``/``ASTRABOX_PORT`` (set
  by containers/server/Dockerfile), with an explicit CLI flag taking
  precedence over the env var. The literal default is ``127.0.0.1:8000`` —
  loopback, since the default identity mode asserts no authentication.
* uvicorn is given the app as the import string
  ``"astrabox.api.app:create_app"`` with ``factory=True`` rather than a
  pre-built instance, so it owns process/worker lifecycle and ``--reload``
  works in development.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

#: Import string for uvicorn's factory mode. Kept as a constant so there is a
#: single source of truth shared by `astrabox serve` and any external
#: `uvicorn astrabox.api.app:create_app --factory` invocation.
APP_FACTORY = "astrabox.api.app:create_app"

# Loopback by default: the default identity mode asserts no authentication and
# the Docker socket is root-equivalent, so a wide bind would expose the admin
# surface to the LAN.
# Set ASTRABOX_HOST=0.0.0.0 explicitly to bind all interfaces (the server
# container does this, since it is namespace-isolated and the host-side
# `-p 127.0.0.1:...:8000` publish is what limits exposure there).
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def register(subparsers: Any) -> None:
    """Add the operator subcommands to the top-level ``astrabox`` parser."""
    serve = subparsers.add_parser(
        "serve",
        help="Run the AstraBox API server (uvicorn).",
        description="Boot the FastAPI application under uvicorn.",
    )
    serve.add_argument(
        "--host",
        default=os.environ.get("ASTRABOX_HOST", _DEFAULT_HOST),
        help="Bind address (default: $ASTRABOX_HOST or %(default)s).",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ASTRABOX_PORT", _DEFAULT_PORT)),
        help="Bind port (default: $ASTRABOX_PORT or %(default)s).",
    )
    serve.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on code changes (development only).",
    )
    serve.add_argument(
        "--log-level",
        default=os.environ.get("ASTRABOX_LOG_LEVEL", "info"),
        help="uvicorn log level (default: $ASTRABOX_LOG_LEVEL or %(default)s).",
    )
    serve.set_defaults(func=_cmd_serve)

    snapshots = subparsers.add_parser(
        "verify-opensandbox-snapshots",
        help="Prove that pause and resume preserve a sandbox file.",
        description=(
            "Create a real sandbox, write a marker, pause it, resume the same "
            "sandbox, read the marker, and remove the test sandbox."
        ),
    )
    snapshots.add_argument(
        "--image",
        help="Sandbox image override (default: $ASTRABOX_AGENT_IMAGE).",
    )
    snapshots.add_argument(
        "--lifecycle-base-url",
        help=(
            "OpenSandbox lifecycle API override. Pass the bundled server's "
            "loopback URL when running through docker/kubectl exec."
        ),
    )
    snapshots.add_argument(
        "--timeout-seconds",
        type=int,
        default=720,
        help="Whole verification deadline, 1-720 seconds (default: %(default)s).",
    )
    snapshots.add_argument(
        "--json-out",
        type=Path,
        help="Write non-secret JSON evidence atomically instead of stdout.",
    )
    snapshots.set_defaults(func=_cmd_verify_opensandbox_snapshots)


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _guard_unauthenticated_bind(host: str) -> None:
    """Refuse to serve an unauthenticated deployment on a non-loopback bind.

    The default identity mode is ``local`` — no auth — and the deployment
    mounts a root-equivalent Docker socket. A public bind would expose remote
    root-equivalent control, so this combination fails during startup.

    Inside a container the bind address proves nothing (0.0.0.0 in-container
    with a loopback-published port is the safe compose default), so there the
    guard degrades to one CRITICAL log line. ``ASTRABOX_ALLOW_UNAUTHENTICATED_BIND=1``
    is the explicit operator override for either case.
    """
    identity = str(os.environ.get("ASTRABOX_WEB_IDENTITY", "") or "").strip().lower()
    if identity not in ("", "local"):
        return
    if str(host or "").strip().lower() in _LOOPBACK_HOSTS:
        return
    if str(os.environ.get("ASTRABOX_ALLOW_UNAUTHENTICATED_BIND", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    message = (
        f"refusing to bind {host!r} with no authentication configured: the default "
        "deployment has no API auth and drives a root-equivalent Docker socket. "
        "Configure an identity resolver (ASTRABOX_WEB_IDENTITY=oidc / trusted_header "
        "/ jwt), bind to 127.0.0.1, or set ASTRABOX_ALLOW_UNAUTHENTICATED_BIND=1 "
        "if this network is genuinely private."
    )
    if os.path.exists("/.dockerenv"):
        # In-container: the published port, not the bind, decides exposure —
        # warn as loudly as a log can and let the operator's publish choice stand.
        from astrabox.common.logger.logger_factory import get_logger

        get_logger(__name__).critical("%s (in-container bind: not refusing)", message)
        return
    raise SystemExit(f"astrabox serve: {message}")


def _cmd_serve(args: argparse.Namespace) -> int:
    """Handle ``astrabox serve`` — start uvicorn on the FastAPI factory.

    ``uvicorn`` is imported lazily (inside the handler) so that merely importing
    this module — e.g. for ``astrabox --help`` or in tests — does not pull the
    ASGI server stack. ``factory=True`` tells uvicorn that ``APP_FACTORY``
    resolves to a callable returning the app, matching
    :func:`astrabox.api.app.create_app`.
    """
    _guard_unauthenticated_bind(args.host)

    import uvicorn

    uvicorn.run(
        APP_FACTORY,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        factory=True,
    )
    return 0


def _cmd_verify_opensandbox_snapshots(args: argparse.Namespace) -> int:
    """Run the live OpenSandbox snapshot acceptance check."""
    from astrabox.deploy.opensandbox_snapshot_check import (
        verify_opensandbox_snapshots,
    )

    result = asyncio.run(
        verify_opensandbox_snapshots(
            image=args.image,
            lifecycle_base_url=args.lifecycle_base_url,
            timeout_seconds=args.timeout_seconds,
        )
    )
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out is None:
        print(payload, end="")
        return 0
    path = Path(args.json_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    return 0
