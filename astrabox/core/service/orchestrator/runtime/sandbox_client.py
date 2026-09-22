"""Sandbox SDK operations.

Stateless helpers for sandbox connect, kill, renew, and introspection.
"""

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# The sandbox SDK (Sandbox.connect/create, SandboxManager) is an optional
# dependency, used only by a backend that talks to a remote sandbox service —
# a backend that drives containers by id through its own control plane never
# reaches the Sandbox.* paths here. None
# sentinels keep the module importing; the functions that use them resolve the
# sandbox SDK at call time (and fail loud there if it is absent). No fallback.
ConnectionConfig = None
Sandbox = None
SandboxManager = None
from opensandbox.models.filesystem import WriteEntry

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.retry_utils import (
    build_retry_warning_before_sleep,
    retry_async_call,
)
# The probe result type + status constants live at the seam (the contract layer);
# re-exported here for existing importers of this module.
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
    SandboxLifecycleProbeResult,
    sandbox_for_sandbox,
)

logger = get_logger(__name__)


_SANDBOX_ENDPOINT_HOST_RE = re.compile(
    r"(?:^|-)(?:sandbox-)?(?P<sandbox_id>[0-9a-f-]{32,36})-(?P<port>\d+)(?:\.|$)",
    re.IGNORECASE,
)
_SANDBOX_ENDPOINT_RESOLVE_MAX_ATTEMPTS = 12
_SANDBOX_ENDPOINT_RESOLVE_WAIT_SECONDS = 2.0

EndpointHeadersLoader = Callable[[str, int], Awaitable[dict[str, str]]]


def _normalize_lifecycle_v1_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise RuntimeError("sandbox lifecycle base_url missing")
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized}"
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def _extract_sandbox_endpoint_target(url: httpx.URL) -> tuple[str, int] | None:
    host = str(url.host or "")
    match = _SANDBOX_ENDPOINT_HOST_RE.search(host)
    if not match:
        return None
    return match.group("sandbox_id"), int(match.group("port"))


class ExecdEndpointAuthTransport(httpx.AsyncBaseTransport):
    """Inject per-sandbox execd endpoint auth headers for all SDK data-plane calls."""

    def __init__(
        self,
        *,
        sandbox_api_key: str,
        base_url: str,
        request_timeout_seconds: int,
        inner: httpx.AsyncBaseTransport | None = None,
        endpoint_headers_loader: EndpointHeadersLoader | None = None,
    ) -> None:
        self._sandbox_api_key = str(sandbox_api_key or "")
        self._lifecycle_v1_base_url = _normalize_lifecycle_v1_base_url(base_url)
        self._request_timeout = float(request_timeout_seconds)
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._endpoint_headers_loader = endpoint_headers_loader or self._load_endpoint_headers
        self._headers_cache: dict[tuple[str, int], dict[str, str]] = {}
        self._headers_lock = asyncio.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = _extract_sandbox_endpoint_target(request.url)
        if target is not None:
            endpoint_headers = await self._get_endpoint_headers(*target)
            for key, value in endpoint_headers.items():
                if key and value and key not in request.headers:
                    request.headers[key] = value
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _get_endpoint_headers(self, sandbox_id: str, port: int) -> dict[str, str]:
        cache_key = (sandbox_id, port)
        cached = self._headers_cache.get(cache_key)
        if cached is not None:
            return cached

        async with self._headers_lock:
            cached = self._headers_cache.get(cache_key)
            if cached is not None:
                return cached
            headers = await self._endpoint_headers_loader(sandbox_id, port)
            normalized = {
                str(key): str(value)
                for key, value in (headers or {}).items()
                if str(key).strip() and value is not None
            }
            if normalized:
                self._headers_cache[cache_key] = normalized
            return normalized

    async def _load_endpoint_headers(self, sandbox_id: str, port: int) -> dict[str, str]:
        if not self._sandbox_api_key:
            raise RuntimeError("sandbox api key missing for execd endpoint auth")

        url = f"{self._lifecycle_v1_base_url}/sandboxes/{sandbox_id}/endpoints/{port}"
        async with httpx.AsyncClient(timeout=self._request_timeout) as client:
            response = await client.get(
                url,
                headers={"OPEN-SANDBOX-API-KEY": self._sandbox_api_key},
            )
            response.raise_for_status()
            payload = response.json()

        endpoint_headers = payload.get("headers") if isinstance(payload, dict) else None
        if not isinstance(endpoint_headers, dict):
            raise RuntimeError(
                "sandbox endpoint response missing headers "
                f"sandbox_id={sandbox_id} port={port}"
            )
        return {
            str(key): str(value)
            for key, value in endpoint_headers.items()
            if str(key).strip() and value is not None
        }


def build_execd_connection_config(
    connection_config_cls: Any,
    *,
    sandbox_api_key: str,
    base_url: str,
    request_timeout_seconds: int,
) -> Any:
    return connection_config_cls(
        api_key=sandbox_api_key,
        domain=base_url,
        request_timeout=timedelta(seconds=request_timeout_seconds),
        transport=ExecdEndpointAuthTransport(
            sandbox_api_key=sandbox_api_key,
            base_url=base_url,
            request_timeout_seconds=request_timeout_seconds,
        ),
    )


def get_underlying_sandbox(sandbox_obj: Any) -> Any | None:
    """Extract the underlying sandbox object from a CodeInterpreter or return as-is."""
    if sandbox_obj is None:
        return None
    underlying = getattr(sandbox_obj, "sandbox", None)
    if underlying is not None and underlying is not sandbox_obj:
        return underlying
    return sandbox_obj


async def create_sandbox_directories(
    sandbox_obj: Any,
    paths: list[str],
    *,
    mode: int = 755,
) -> None:
    cleaned_paths = [str(path or "").strip() for path in paths if str(path or "").strip()]
    if not cleaned_paths:
        return

    candidates = []
    underlying = get_underlying_sandbox(sandbox_obj)
    if underlying is not None:
        candidates.append(underlying)
    if sandbox_obj is not None and sandbox_obj is not underlying:
        candidates.append(sandbox_obj)

    files_api = None
    for candidate in candidates:
        files_api = getattr(candidate, "files", None)
        if files_api is not None:
            break
    create_directories = getattr(files_api, "create_directories", None)
    if not callable(create_directories):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="sandbox files create_directories API not available",
            status_code=502,
        )

    entries = [
        WriteEntry(path=path, data=None, mode=mode, owner=None, group=None, encoding="utf-8")
        for path in cleaned_paths
    ]
    try:
        await create_directories(entries)
    except APIError:
        raise
    except Exception as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"failed to create sandbox directories: {exc}",
            status_code=502,
        ) from exc


def extract_sandbox_id(sandbox: Any) -> str | None:
    for key in ("code_interpreter_id", "sandbox_id", "id"):
        value = getattr(sandbox, key, None)
        if value:
            return str(value)
    return None


def resolve_remote_client(agent: Any) -> Any:
    client_getter = getattr(agent, "client", None)
    if not callable(client_getter):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="remote-agent client getter not found",
            status_code=502,
        )
    client = client_getter()
    if client is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="remote-agent client not initialized",
            status_code=502,
        )
    return client


def _resolve_sandbox_service(manager: Any) -> Any:
    service = getattr(manager, "_sandbox_service", None)
    if service is not None:
        return service
    delegate = getattr(manager, "_delegate", None)
    if delegate is not None:
        service = getattr(delegate, "_sandbox_service", None)
        if service is not None:
            return service
    raise RuntimeError("sandbox manager missing sandbox service")


async def connect_sandbox(
    sandbox_id: str,
    *,
    sandbox_api_key: str,
    base_url: str,
    request_timeout_seconds: int,
) -> Any:
    """Connect to a sandbox without starting a Claude Code agent."""
    connection = build_execd_connection_config(
        ConnectionConfig,
        sandbox_api_key=sandbox_api_key,
        base_url=base_url,
        request_timeout_seconds=request_timeout_seconds,
    )
    try:
        return await Sandbox.connect(sandbox_id, connection_config=connection)
    except BaseException as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"failed to connect to sandbox {sandbox_id}: {exc}",
            status_code=502,
        ) from exc


async def get_sandbox_expires_at(
    sandbox_id: str,
    *,
    sandbox_api_key: str,
    base_url: str,
    request_timeout_seconds: int = 10,
) -> datetime | None:
    if not sandbox_api_key:
        return None

    manager = None
    try:
        connection = build_execd_connection_config(
            ConnectionConfig,
            sandbox_api_key=sandbox_api_key,
            base_url=base_url,
            request_timeout_seconds=request_timeout_seconds,
        )
        manager = await SandboxManager.create(connection_config=connection)
        info = await manager.get_sandbox_info(sandbox_id)
        expires_at = getattr(info, "expires_at", None)
        if isinstance(expires_at, datetime):
            return expires_at
    except Exception as exc:
        logger.warning("failed to get sandbox expires_at for %s: %s", sandbox_id, exc)
    finally:
        if manager is not None:
            try:
                await manager.close()
            except Exception:
                pass
    return None


def _normalize_sandbox_state(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _is_sandbox_not_found_exception(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return True

    error = getattr(exc, "error", None)
    error_status_code = getattr(error, "status_code", None)
    if error_status_code == 404:
        return True
    error_code = str(getattr(error, "code", "") or "").strip().upper()
    if error_code == "NOT_FOUND":
        return True
    return False


def _should_retry_sandbox_probe_exception(exc: Exception) -> bool:
    if _is_sandbox_not_found_exception(exc):
        return False
    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 429}:
        return True
    error = getattr(exc, "error", None)
    error_status_code = getattr(error, "status_code", None)
    if error_status_code in {408, 429}:
        return True
    if isinstance(status_code, int):
        return status_code >= 500
    if isinstance(error_status_code, int):
        return error_status_code >= 500
    retryable_transport_errors: tuple[type[BaseException], ...] = (
        ConnectionError,
        OSError,
        TimeoutError,
    )
    with contextlib.suppress(Exception):
        import httpx

        retryable_transport_errors = retryable_transport_errors + (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
        )
    return isinstance(exc, retryable_transport_errors)


def _should_retry_sandbox_endpoint_exception(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = int(getattr(exc.response, "status_code", 0) or 0)
        return status_code in {408, 429} or status_code >= 500
    return isinstance(
        exc,
        (
            ConnectionError,
            OSError,
            TimeoutError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
        ),
    )


async def probe_sandbox_lifecycle(
    sandbox_id: str,
    *,
    sandbox_api_key: str,
    base_url: str,
    request_timeout_seconds: int = 10,
) -> SandboxLifecycleProbeResult:
    target = str(sandbox_id or "").strip()
    if not target:
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
            error_text="sandbox_id missing",
        )
    if not sandbox_api_key:
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
            error_text="sandbox api key missing",
        )

    async def _load_sandbox_state() -> str | None:
        # Remote-sandbox path: the lifecycle probe asks a remote
        # ``SandboxManager`` for a sandbox's state. The sandbox SDK is an optional
        # dependency; the module-level ``ConnectionConfig`` /
        # ``SandboxManager`` sentinels are ``None`` here. A backend that probes
        # container liveness through its own control plane never reaches this
        # SandboxManager call, so fail loud at call time rather than resolving the
        # optional package (no silent fallback, module still imports).
        if ConnectionConfig is None or SandboxManager is None:
            raise NotImplementedError(
                "probe_sandbox_lifecycle's remote SandboxManager probe requires a "
                "remote-sandbox backend; the built-in backend reports liveness via "
                "its own provider probe (container inspect) instead. This path is "
                "only reached by a backend that talks to a remote sandbox service."
            )
        _ConnectionConfig = ConnectionConfig
        _SandboxManager = SandboxManager

        manager = None
        try:
            connection = build_execd_connection_config(
                _ConnectionConfig,
                sandbox_api_key=sandbox_api_key,
                base_url=base_url,
                request_timeout_seconds=request_timeout_seconds,
            )
            manager = await _SandboxManager.create(connection_config=connection)
            info = await manager.get_sandbox_info(target)
            status = getattr(info, "status", None)
            return _normalize_sandbox_state(
                getattr(status, "state", None) or getattr(info, "state", None)
            )
        finally:
            if manager is not None:
                with contextlib.suppress(Exception):
                    await manager.close()

    try:
        sandbox_state = await retry_async_call(
            _load_sandbox_state,
            should_retry_exception=_should_retry_sandbox_probe_exception,
            max_attempts=3,
            wait_seconds=0.5,
            before_sleep=build_retry_warning_before_sleep(
                logger,
                lambda retry_state, exc: (
                    "sandbox lifecycle probe retry sandbox=%s attempt=%s err=%s"
                    % (target, retry_state.attempt_number, exc)
                ),
            ),
        )
    except Exception as exc:
        if _is_sandbox_not_found_exception(exc):
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
                error_text=str(exc),
            )
        logger.warning(
            "sandbox lifecycle probe failed sandbox=%s err=%s",
            target,
            exc,
        )
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
            error_text=str(exc),
        )

    if not sandbox_state:
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
            error_text=f"sandbox {target} returned empty lifecycle state",
        )

    return SandboxLifecycleProbeResult(
        probe_status=SANDBOX_LIFECYCLE_PROBE_OK,
        sandbox_state=sandbox_state,
    )


async def renew_sandbox_by_id(
    sandbox_id: str,
    *,
    ttl_seconds: int,
    sandbox_api_key: str,
    base_url: str,
    request_timeout_seconds: int = 10,
) -> datetime | None:
    target = str(sandbox_id or "").strip()
    if not target or not sandbox_api_key:
        return None

    manager = None
    try:
        connection = build_execd_connection_config(
            ConnectionConfig,
            sandbox_api_key=sandbox_api_key,
            base_url=base_url,
            request_timeout_seconds=request_timeout_seconds,
        )
        manager = await SandboxManager.create(connection_config=connection)
        info = await manager.get_sandbox_info(target)
        created_at = getattr(info, "created_at", None)
        current_expires_at = getattr(info, "expires_at", None)
        if not isinstance(created_at, datetime):
            raise RuntimeError(f"sandbox {target} missing created_at")

        target_expires_at = created_at + timedelta(seconds=ttl_seconds)
        if (
            isinstance(current_expires_at, datetime)
            and current_expires_at >= target_expires_at
        ):
            return current_expires_at

        now = datetime.now(target_expires_at.tzinfo or timezone.utc)
        if target_expires_at <= now:
            raise RuntimeError(
                f"sandbox {target} target expiration already elapsed: {target_expires_at.isoformat()}"
            )

        # The platform renew API expects an absolute expiration timestamp.
        sandbox_service = _resolve_sandbox_service(manager)
        result = await sandbox_service.renew_sandbox_expiration(
            target,
            target_expires_at,
        )
        expires_at = getattr(result, "expires_at", None)
        if isinstance(expires_at, datetime):
            return expires_at
    except Exception as exc:
        logger.warning("failed to renew sandbox by id=%s err=%s", target, exc)
        raise
    finally:
        if manager is not None:
            try:
                await manager.close()
            except Exception:
                pass
    return None


async def resolve_enhanced_server_endpoint(
    sandbox_obj: Any,
    *,
    sandbox_id: str | None = None,
    port: int = 8000,
    connect_fn: Any = None,
) -> str | None:
    """Resolve the enhanced server endpoint for a sandbox.

    *connect_fn* is an async callable(sandbox_id) -> Sandbox used when the
    in-memory sandbox is unavailable and a transient connection is needed.
    """
    sandbox = get_underlying_sandbox(sandbox_obj)
    transient_sandbox = None

    if sandbox is None:
        effective_id = str(sandbox_id or "").strip()
        if not effective_id or connect_fn is None:
            return None
        try:
            transient_sandbox = await connect_fn(effective_id)
            sandbox = get_underlying_sandbox(transient_sandbox)
        except Exception as exc:
            logger.warning(
                "failed to resolve enhanced server endpoint: sandbox=%s err=%s",
                effective_id,
                exc,
            )
            return None

    if sandbox is None:
        return None

    try:
        endpoint = await sandbox.get_endpoint(port)
        endpoint_value = str(getattr(endpoint, "endpoint", "") or "").strip()
        return endpoint_value or None
    except Exception as exc:
        logger.warning(
            "failed to get enhanced server endpoint: sandbox=%s port=%s err=%s",
            sandbox_id or extract_sandbox_id(sandbox),
            port,
            exc,
        )
        return None
    finally:
        transient_underlying = get_underlying_sandbox(transient_sandbox)
        close = getattr(transient_underlying, "close", None) if transient_underlying else None
        if callable(close):
            with contextlib.suppress(Exception):
                await close()


async def resolve_sandbox_endpoint_by_id(
    sandbox_id: str,
    *,
    port: int = 8000,
    sandbox_api_key: str,
    base_url: str,
    request_timeout_seconds: int,
) -> str | None:
    """Resolve a sandbox endpoint through the lifecycle API without execd attach."""
    target = str(sandbox_id or "").strip()
    if not target:
        return None
    if not str(sandbox_api_key or "").strip():
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="sandbox api key missing for endpoint resolution",
            status_code=500,
        )

    lifecycle_base_url = _normalize_lifecycle_v1_base_url(base_url)
    url = f"{lifecycle_base_url}/sandboxes/{target}/endpoints/{int(port)}"
    try:
        async def _load_endpoint_payload() -> Any:
            async with httpx.AsyncClient(timeout=float(request_timeout_seconds)) as client:
                response = await client.get(
                    url,
                    headers={"OPEN-SANDBOX-API-KEY": str(sandbox_api_key).strip()},
                )
                response.raise_for_status()
                return response.json()

        payload = await retry_async_call(
            _load_endpoint_payload,
            should_retry_exception=_should_retry_sandbox_endpoint_exception,
            max_attempts=_SANDBOX_ENDPOINT_RESOLVE_MAX_ATTEMPTS,
            wait_seconds=_SANDBOX_ENDPOINT_RESOLVE_WAIT_SECONDS,
            before_sleep=build_retry_warning_before_sleep(
                logger,
                lambda state, exc: (
                    "retry sandbox endpoint resolution "
                    f"sandbox={target} port={int(port)} "
                    f"attempt={state.attempt_number} err={exc}"
                ),
            ),
        )
    except Exception as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "failed to resolve sandbox endpoint "
                f"sandbox={target} port={int(port)} "
                f"after {_SANDBOX_ENDPOINT_RESOLVE_MAX_ATTEMPTS} attempts: {exc}"
            ),
            status_code=502,
        ) from exc

    if not isinstance(payload, dict):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"sandbox endpoint response is invalid sandbox={target} port={int(port)}",
            status_code=502,
        )
    endpoint = str(payload.get("endpoint") or "").strip()
    return endpoint or None


async def check_server_health(sandbox: Any, port: int, timeout: float = 5.0) -> bool:
    try:
        endpoint = await sandbox.get_endpoint(port)
        dataplane = sandbox_for_sandbox(sandbox).build_dataplane(
            sandbox=sandbox, endpoint=getattr(endpoint, "endpoint", None), port=port
        )
        resp = await dataplane.request("GET", "/health", timeout=timeout)
        return resp.status_code == 200 and resp.text == "OK"
    except Exception:
        return False


async def reset_server_session(
    sandbox: Any,
    port: int,
    session_id: str,
    timeout: float = 5.0,
) -> None:
    endpoint = await sandbox.get_endpoint(port)
    dataplane = sandbox_for_sandbox(sandbox).build_dataplane(
        sandbox=sandbox, endpoint=getattr(endpoint, "endpoint", None), port=port
    )
    resp = await dataplane.request("GET", f"/reset/{session_id}", timeout=timeout)
    if resp.status_code != 200:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"failed to reset remote session {session_id}: {resp.status_code}",
            status_code=502,
        )
