"""The path below which browsers and API clients reach this deployment.

A gateway can publish AstraBox below a path of a host it shares with other
applications, such as ``https://example.com/astrabox/``. AstraBox still routes
every request by the path below that prefix: a reverse proxy removes the prefix
before forwarding, or an ASGI host mounts the application at it (Starlette's
``Mount``, which records the prefix as the request's ``root_path``).
:mod:`astrabox.web.public_path_middleware` refuses a request that arrives any
other way.

The request cannot tell AstraBox the prefix a proxy removed, so the deployment
states it in ``ASTRABOX_PUBLIC_PATH_PREFIX``. Every URL AstraBox gives a browser
or an API client carries it: the console's page and its references, sign-in
callbacks and redirects, cookie paths, and the share, file, Agent and channel
URLs it composes.
"""

from __future__ import annotations

import os
import re

from starlette.requests import HTTPConnection

PUBLIC_PATH_PREFIX_ENV = "ASTRABOX_PUBLIC_PATH_PREFIX"

# Letters, digits and the unreserved punctuation of RFC 3986. A prefix made of
# these needs no escaping in an HTML attribute, a cookie Path or a URL.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~-]+")


def public_path_prefix() -> str:
    """Return the configured prefix: ``""`` at the root, else a path like ``/astrabox``.

    The value must be empty or one or more segments, each after a slash, with
    no trailing slash and no ``.`` or ``..`` segment. One spelling is accepted
    so the server, the installer and the identity provider's seeded callback
    cannot disagree about it. ``create_app`` reads it once at construction, so
    an invalid value stops the server from starting.

    Raises:
        ValueError: the value is not a valid prefix.
    """

    raw = str(os.getenv(PUBLIC_PATH_PREFIX_ENV, "") or "").strip()
    if not raw:
        return ""
    head, *segments = raw.split("/")
    if (
        head
        or not segments
        or any(
            not _SEGMENT_RE.fullmatch(segment) or segment in {".", ".."}
            for segment in segments
        )
    ):
        raise ValueError(
            f"{PUBLIC_PATH_PREFIX_ENV} must be empty, for the root of the host, or a "
            "path such as /astrabox: a leading slash, no trailing slash, and "
            "segments of letters, digits, '.', '_', '~' or '-' other than '.' and "
            f"'..'; got {raw!r}"
        )
    return raw


def public_path(path: str) -> str:
    """Return *path*, an AstraBox path such as ``/api/v1/auth/callback``, as browsers address it.

    *path* is ``""`` for the prefix itself, or begins with ``/``.
    """

    if path and not path.startswith("/"):
        raise ValueError(f"an AstraBox path begins with '/': {path!r}")
    return public_path_prefix() + path


def public_url(connection: HTTPConnection, path: str) -> str:
    """Return the absolute URL a browser uses for *path* on this deployment.

    The scheme and host come from the request, and the prefix from
    configuration. ``request.base_url`` is not used: Starlette builds it from
    the top-level application's root path, which carries neither a prefix the
    proxy removed nor the path an ASGI host mounted AstraBox at.
    """

    return f"{connection.url.scheme}://{connection.url.netloc}{public_path(path)}"


__all__ = [
    "PUBLIC_PATH_PREFIX_ENV",
    "public_path",
    "public_path_prefix",
    "public_url",
]
