"""The HTTP client the client subcommands talk to a deployment through.

One place resolves the endpoint and the credential, unwraps the
``{"code", "message", "data"}`` envelope every ``/api/v1`` handler returns
(:mod:`astrabox.api.routes.response_envelope`), and maps a failure onto the
exit code a caller branches on.

The deployment's own error code travels out of here untouched. The CLI adds no
error vocabulary for anything the API already names — see
``docs/maintainers/error-code-registry-gate.md``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from astrabox.cli.output import (
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_FAILED,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    CliError,
)

#: Host port the maintained local Compose deployment publishes AstraBox on.
#: The name and the fallback are the ones ``containers/compose.yaml`` uses in
#: its publish rule (``127.0.0.1:${ASTRABOX_SERVER_HOST_PORT:-8088}:8000``), so
#: an operator who moves the published port moves it for both.
#:
#: This is the *host* port, not ``ASTRABOX_PORT``: that one is the address the
#: app binds inside its container, which a client on the host cannot reach.
#: A deployment started with ``astrabox serve`` directly binds ``ASTRABOX_PORT``
#: on the host instead, and needs ``--endpoint``/``ASTRABOX_ENDPOINT``.
_DEFAULT_HOST_PORT = "8088"

#: Whole-request deadline. Authoring calls are small reads and writes; a
#: deployment that has not answered in this long is down, not slow.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: The envelope's success code. Any other value is a failure the handler chose
#: to report inside a 2xx body.
_ENVELOPE_OK = "OK"


def default_endpoint() -> str:
    """The maintained local deployment's address on this host.

    Read at call time rather than frozen at import so a process that sets
    ``ASTRABOX_SERVER_HOST_PORT`` — including ``main`` loading it from ``.env``
    — is honoured.
    """
    port = os.environ.get("ASTRABOX_SERVER_HOST_PORT") or _DEFAULT_HOST_PORT
    return f"http://127.0.0.1:{port}"


@dataclass(frozen=True)
class Endpoint:
    """A resolved deployment address and the credential to reach it with."""

    base_url: str
    token: str | None

    def headers(self) -> dict[str, str]:
        """Request headers, carrying the bearer token when there is one."""
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}


def resolve_endpoint(
    *,
    endpoint: str | None = None,
    token: str | None = None,
) -> Endpoint:
    """Resolve the deployment address and credential from flags and env vars.

    Address precedence: ``--endpoint``, ``ASTRABOX_ENDPOINT``,
    :func:`default_endpoint`.

    Credential precedence: ``--token``, ``ASTRABOX_TOKEN``, then an OAuth
    client-credentials exchange when ``ASTRABOX_CLIENT_ID``,
    ``ASTRABOX_CLIENT_SECRET`` and ``ASTRABOX_TOKEN_URL`` are all set. With none
    of these the request is sent unauthenticated, which is what the default
    local identity mode expects and what any other mode answers ``401`` to.

    A partially configured client credential — two of the three variables — is
    refused rather than silently downgraded to an unauthenticated request,
    which would surface as a confusing ``401`` instead of the missing variable.
    """
    base_url = (endpoint or os.environ.get("ASTRABOX_ENDPOINT") or default_endpoint()).rstrip("/")
    resolved_token = token or os.environ.get("ASTRABOX_TOKEN") or None
    if resolved_token:
        return Endpoint(base_url=base_url, token=resolved_token)

    client_id = os.environ.get("ASTRABOX_CLIENT_ID") or ""
    client_secret = os.environ.get("ASTRABOX_CLIENT_SECRET") or ""
    token_url = os.environ.get("ASTRABOX_TOKEN_URL") or ""
    provided = [bool(client_id), bool(client_secret), bool(token_url)]
    if not any(provided):
        return Endpoint(base_url=base_url, token=None)
    if not all(provided):
        missing = [
            name
            for name, value in (
                ("ASTRABOX_CLIENT_ID", client_id),
                ("ASTRABOX_CLIENT_SECRET", client_secret),
                ("ASTRABOX_TOKEN_URL", token_url),
            )
            if not value
        ]
        raise CliError(
            "incomplete client credential: " + ", ".join(missing) + " not set",
            exit_code=EXIT_USAGE,
        )
    return Endpoint(
        base_url=base_url,
        token=_exchange_client_credentials(
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=os.environ.get("ASTRABOX_SCOPE") or None,
        ),
    )


def _exchange_client_credentials(
    *, token_url: str, client_id: str, client_secret: str, scope: str | None
) -> str:
    """Exchange an OAuth client credential for a short-lived bearer token.

    ``scope`` is sent only when ``ASTRABOX_SCOPE`` is set. Omitting it lets the
    identity provider issue whatever the client is registered for, rather than
    the CLI asserting a scope set the deployment may not grant.
    """
    import httpx

    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        form["scope"] = scope
    try:
        response = httpx.post(token_url, data=form, timeout=DEFAULT_TIMEOUT_SECONDS)
    except httpx.RequestError as exc:
        raise CliError(
            f"token endpoint unreachable at {token_url}: {exc}",
            exit_code=EXIT_UNREACHABLE,
        ) from exc
    if response.status_code >= 400:
        raise CliError(
            f"client-credentials exchange refused with HTTP {response.status_code}",
            exit_code=EXIT_AUTH,
            details={"token_url": token_url, "body": response.text[:500]},
        )
    try:
        access_token = str(response.json()["access_token"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CliError(
            "token endpoint returned no access_token",
            exit_code=EXIT_AUTH,
            details={"token_url": token_url},
        ) from exc
    return access_token


def as_items(payload: Any) -> list[Mapping[str, Any]]:
    """Normalise a collection payload into a list of documents.

    A list route answers with a list; a few answer with a single-key wrapper
    around one (``{"mcp_servers": [...]}``). Anything else is a shape a caller
    cannot list, and saying so beats rendering an empty table.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for value in payload.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        return [payload]
    raise CliError(f"unexpected response shape: {type(payload).__name__}")


class ApiClient:
    """Envelope-aware HTTP calls against one deployment.

    Every method returns the envelope's ``data`` payload and raises
    :class:`~astrabox.cli.output.CliError` for anything else, so callers hold no
    status-code logic of their own.

    ``transport`` is httpx's own transport seam, passed through so a caller can
    supply one; the default ``None`` lets httpx build its network transport.
    """

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Any = None,
    ) -> None:
        import httpx

        self._endpoint = endpoint
        self._client = httpx.Client(
            base_url=endpoint.base_url,
            headers=endpoint.headers(),
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    @property
    def base_url(self) -> str:
        """The deployment address requests are sent to."""
        return self._endpoint.base_url

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Read one resource or collection."""
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json: Any = None) -> Any:
        """Create a resource, or invoke an action route."""
        return self._request("POST", path, json=json)

    def put(self, path: str, *, json: Any = None) -> Any:
        """Replace a resource in full."""
        return self._request("PUT", path, json=json)

    def delete(self, path: str) -> Any:
        """Remove a resource."""
        return self._request("DELETE", path)

    @contextmanager
    def stream(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        read_timeout: float | None = None,
    ) -> Iterator[Any]:
        """Open a streaming response, yielding httpx's response object.

        Unlike the other methods this does not unwrap an envelope: a streaming
        route answers with Server-Sent Events, and reading the body to decode
        one would defeat the streaming. The caller therefore handles the status
        code, and only transport failures are translated here.

        ``read_timeout`` bounds the gap between chunks rather than the whole
        response, so a turn that runs for minutes is not cut off while a dead
        transport still is.
        """
        import httpx

        timeout = None
        if read_timeout is not None:
            timeout = httpx.Timeout(DEFAULT_TIMEOUT_SECONDS, read=read_timeout)
        try:
            with self._client.stream(
                method, path, json=json, timeout=timeout or httpx.USE_CLIENT_DEFAULT
            ) as response:
                yield response
        except httpx.RequestError as exc:
            raise CliError(
                f"deployment unreachable at {self._endpoint.base_url}: {exc}",
                exit_code=EXIT_UNREACHABLE,
                details={"endpoint": self._endpoint.base_url},
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        import httpx

        try:
            response = self._client.request(method, path, json=json, params=params)
        except httpx.RequestError as exc:
            raise CliError(
                f"deployment unreachable at {self._endpoint.base_url}: {exc}",
                exit_code=EXIT_UNREACHABLE,
                details={"endpoint": self._endpoint.base_url},
            ) from exc
        return self._unwrap(response, method=method, path=path)

    def _unwrap(self, response: Any, *, method: str, path: str) -> Any:
        """Turn one HTTP response into a payload or a :class:`CliError`.

        A ``204`` carries no envelope — several delete routes answer with it —
        so it resolves to ``None`` rather than a decode failure.
        """
        if response.status_code == 204:
            return None

        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None

        code = None
        message = None
        if isinstance(body, dict):
            code = body.get("code")
            message = body.get("message")

        if response.status_code < 400 and code in (None, _ENVELOPE_OK):
            if isinstance(body, dict) and "data" in body:
                return body["data"]
            return body

        raise CliError(
            _failure_message(
                method=method,
                path=path,
                status=response.status_code,
                message=message,
                body=response.text,
            ),
            exit_code=_exit_code_for(response.status_code),
            code=str(code) if code else None,
            details={"status": response.status_code, "path": path},
        )


def _failure_message(
    *, method: str, path: str, status: int, message: Any, body: str
) -> str:
    """Compose the one-line failure, preferring the deployment's own message."""
    detail = str(message).strip() if message else ""
    if not detail:
        detail = body.strip()[:300] or "no response body"
    return f"{method} {path} failed with HTTP {status}: {detail}"


def _exit_code_for(status: int) -> int:
    """Map an HTTP status onto the exit code that tells a caller what to fix.

    ``401``/``403`` mean fix the credential, ``409`` means the document raced
    another writer and must be re-read, and everything else means the request
    itself was refused.
    """
    if status in (401, 403):
        return EXIT_AUTH
    if status == 409:
        return EXIT_CONFLICT
    return EXIT_FAILED


__all__ = [
    "ApiClient",
    "as_items",
    "DEFAULT_TIMEOUT_SECONDS",
    "Endpoint",
    "default_endpoint",
    "resolve_endpoint",
]
