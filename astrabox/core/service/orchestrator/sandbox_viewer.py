"""Mint image-owned viewer grants behind the sandbox's native endpoint."""

from __future__ import annotations

import json
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from astrabox.common.utils.errors import APIError


async def mint_viewer_url(
    sandbox: Any,
    endpoint: dict[str, Any],
    *,
    arguments: list[str],
    prefix: str,
    error_code: str,
) -> dict[str, str]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=20)
    if endpoint["expires_at"]:
        expires = min(expires, datetime.fromisoformat(endpoint["expires_at"]))
    expires = datetime.fromtimestamp(int(expires.timestamp()), tz=timezone.utc)
    result = await sandbox.commands.run(shlex.join([
        "/usr/local/bin/astrabox-workspace-viewer", *arguments,
        str(int(expires.timestamp())),
    ]))
    if result.exit_code != 0:
        raise APIError(
            code=error_code,
            message=f"sandbox viewer unavailable: {result.output[:1000]}",
            status_code=502,
        )
    try:
        path = json.loads(result.stdout)["path"]
        if not isinstance(path, str) or not path.startswith(prefix):
            raise ValueError("invalid viewer path")
    except (ValueError, KeyError, TypeError) as exc:
        raise APIError(
            code=error_code, message="sandbox returned an invalid viewer grant", status_code=502,
        ) from exc
    # OpenSandbox owns the routing prefix and any gateway query parameters.
    base = urlsplit(endpoint["url"])
    url = urlunsplit(base._replace(path=f"{base.path.rstrip('/')}/{path}"))
    return {"url": url, "expires_at": expires.isoformat()}
