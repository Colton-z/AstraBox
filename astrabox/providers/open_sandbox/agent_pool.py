"""OpenSandbox's official client pool behind AstraBox's sandbox seam.

OpenSandbox owns Redis coordination, leader election, replenishment, atomic
acquisition, and namespace destruction. This adapter supplies the provider's
technical member identity and bridges the SDK's ``Sandbox`` objects to the
opaque handles used by the platform callbacks. It deliberately has no Agent,
engine, credential, network, or image-construction policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
import threading
from datetime import timedelta
from typing import Any
from uuid import uuid4

from opensandbox import Sandbox, SandboxPoolAsync
from opensandbox.async_redis_pool_store import AsyncRedisPoolStateStore
from opensandbox.exceptions import (
    PoolAcquireFailedException,
    PoolDestroyedException,
    PoolEmptyException,
    PoolNotRunningException,
    PoolStateStoreUnavailableException,
)
from opensandbox.pool import AcquirePolicy, PoolCreationSpec
from opensandbox.pool_manager import SandboxPoolManagerAsync
from opensandbox.pool_types import PoolDestroyOptions

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.providers.open_sandbox import _config
from astrabox.seams.sandbox import (
    SandboxClientPoolCreator,
    SandboxClientPoolMember,
    SandboxClientPoolPreparer,
    SandboxClientPoolSpec,
    SandboxClientPoolStatus,
)

logger = get_logger(__name__)

# A retired name is briefly fenced so a replica with stale local state cannot
# recreate the namespace while its peer is destroying it. Pool names are
# deterministic and may be selected again, so the vendor's seven-day default
# would be too long here.
_POOL_TOMBSTONE_TTL = timedelta(minutes=5)

# The SDK replenishes on its reconcile tick and offers no public way to start
# one, so a claimed member is replaced within one interval.
_RECONCILE_SECONDS = 2

# Members prepared concurrently per pool. A refill of a large pool starts this
# many creates at once instead of all of them, so one Agent's refill cannot
# exhaust a single Docker host or node. The SDK creates at most the deficit, and
# a resize changes only the pool's size, so the limit is fixed rather than
# derived from the size the pool started with.
_WARMUP_CONCURRENCY_LIMIT = 8

# The replica that replenishes a pool holds the SDK's primary lock, which the
# SDK renews only between commits. A lock long enough to outlast one create and
# its preparation left a pool unreplenished that long after its holder died, so
# a heartbeat renews the lock while this process holds it and the lock outlasts
# only the heartbeat. It stays above the SDK's warmup ready timeout, which the
# SDK requires of it.
_PRIMARY_LOCK_HEARTBEAT_SECONDS = 10
_PRIMARY_LOCK_TTL_MARGIN_SECONDS = 30

_POOL_ACQUIRE_FAILURES = (
    PoolAcquireFailedException,
    PoolDestroyedException,
    PoolNotRunningException,
    PoolStateStoreUnavailableException,
)


def _error_detail(exc: BaseException) -> str:
    secret: str | None = None
    with contextlib.suppress(Exception):
        secret = _config.resolve_api_key(load_astrabox_settings())
    return _config.scrub_secret(str(exc), secret=secret)


def _pool_unavailable(
    pool_name: str,
    operation: str,
    exc: BaseException,
) -> APIError:
    return APIError(
        code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
        message=(
            f"OpenSandbox client pool {pool_name!r} could not {operation}: "
            f"{type(exc).__name__}: {_error_detail(exc)}"
        ),
        status_code=503,
    )


def _client_pool_session_id(pool_name: str) -> str:
    """Return a unique synthetic session id valid as a Kubernetes label value."""

    pool_hash = hashlib.sha256(pool_name.encode("utf-8")).hexdigest()[:12]
    return f"client-pool-{pool_hash}-{uuid4().hex[:24]}"


def _pool_member(spec: SandboxClientPoolSpec) -> SandboxClientPoolMember:
    return SandboxClientPoolMember(
        pool_name=spec.pool_name,
        session_id=_client_pool_session_id(spec.pool_name),
        assignment_id=str(uuid4()),
    )


class OpenSandboxClientPoolRegistry:
    """One event loop's official OpenSandbox client-pool schedulers."""

    def __init__(
        self,
        *,
        transport: Any = None,
        state_store: Any = None,
        redis_client: Any = None,
        pool_factory: Any = SandboxPoolAsync,
    ) -> None:
        self._transport = transport
        self._state_store = state_store
        self._redis = redis_client
        self._pool_factory = pool_factory
        self._pools: dict[str, Any] = {}
        self._sizes: dict[str, int] = {}
        self._heartbeats: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        # Stable across restarts of this process on the same host, so a restart
        # takes back the lock it held instead of waiting for it to expire; the
        # SDK grants the primary lock again to the owner that already holds it.
        # The thread names the event loop, since each loop runs its own pools.
        self._owner_id = (
            f"{os.environ.get('HOSTNAME') or socket.gethostname()}"
            f":{threading.current_thread().name}"
        )

    def _connection_config(self) -> Any:
        return _config.sdk_connection_config(load_astrabox_settings(), transport=self._transport)

    async def _ensure_state_store(self) -> Any:
        if self._state_store is not None:
            return self._state_store
        settings = load_astrabox_settings()
        redis_url = str(getattr(settings, "agent_prewarm_redis_url", "") or "").strip()
        if not redis_url:
            raise APIError(
                code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
                message=("OpenSandbox client pools require ASTRABOX_AGENT_PREWARM_REDIS_URL"),
                status_code=503,
            )
        from redis.asyncio import Redis

        self._redis = Redis.from_url(redis_url)
        self._state_store = AsyncRedisPoolStateStore(
            self._redis,
            key_prefix="astrabox:opensandbox:client-pool",
        )
        return self._state_store

    async def ensure(
        self,
        spec: SandboxClientPoolSpec,
        *,
        creator: SandboxClientPoolCreator,
        preparer: SandboxClientPoolPreparer,
    ) -> None:
        """Start or join the official pool without waiting for its first member.

        A pool already running in this event loop is resized to ``max_idle``;
        the SDK keeps the size in shared state, so every replica's scheduler
        refills toward it.
        """

        async with self._lock:
            running = self._pools.get(spec.pool_name)
            if running is not None:
                if self._sizes.get(spec.pool_name) != int(spec.max_idle):
                    await running.resize(int(spec.max_idle))
                    self._sizes[spec.pool_name] = int(spec.max_idle)
                return
            state_store = await self._ensure_state_store()
            settings = load_astrabox_settings()
            lock_ttl = timedelta(
                seconds=int(settings.sandbox_ready_timeout_seconds)
                + _PRIMARY_LOCK_TTL_MARGIN_SECONDS
            )
            pool = self._pool_factory(
                pool_name=spec.pool_name,
                max_idle=spec.max_idle,
                owner_id=self._owner_id,
                state_store=state_store,
                connection_config=self._connection_config(),
                # The SDK requires this even when the custom creator below
                # owns the actual create operation.
                creation_spec=PoolCreationSpec(image=spec.creation_image),
                warmup_concurrency=_WARMUP_CONCURRENCY_LIMIT,
                primary_lock_ttl=lock_ttl,
                reconcile_interval=timedelta(seconds=_RECONCILE_SECONDS),
                warmup_ready_timeout=timedelta(seconds=int(settings.sandbox_ready_timeout_seconds)),
                warmup_skip_health_check=True,
                warmup_sandbox_preparer=self._preparer(spec, preparer),
                idle_timeout=timedelta(seconds=int(spec.idle_timeout_seconds)),
                drain_timeout=timedelta(seconds=30),
                sandbox_creator=self._creator(spec, creator),
            )
            try:
                await pool.start()
            except asyncio.CancelledError:
                logger.warning(
                    "OpenSandbox client-pool start cancelled: pool=%s",
                    spec.pool_name,
                )
                raise
            except PoolDestroyedException as exc:
                logger.error(
                    "OpenSandbox client-pool name remains fenced: pool=%s error=%s",
                    spec.pool_name,
                    _error_detail(exc),
                )
                raise _pool_unavailable(
                    spec.pool_name,
                    "start because its retired namespace remains fenced",
                    exc,
                ) from exc
            except APIError:
                raise
            except Exception as exc:
                logger.error(
                    "OpenSandbox client-pool state initialization failed: "
                    "pool=%s error_type=%s error=%s",
                    spec.pool_name,
                    type(exc).__name__,
                    _error_detail(exc),
                )
                raise _pool_unavailable(
                    spec.pool_name,
                    "initialize its shared state",
                    exc,
                ) from exc
            self._pools[spec.pool_name] = pool
            self._sizes[spec.pool_name] = int(spec.max_idle)
            self._heartbeats[spec.pool_name] = asyncio.create_task(
                self._renew_primary_lock(spec.pool_name, state_store, lock_ttl),
                name=f"client-pool-lock-heartbeat-{spec.pool_name}",
            )

        logger.info(
            "OpenSandbox client-pool scheduler started: pool=%s",
            spec.pool_name,
        )

    async def _renew_primary_lock(
        self, pool_name: str, state_store: Any, ttl: timedelta
    ) -> None:
        """Keep the primary lock alive while this process holds it.

        Renewal succeeds only for the current holder, so a replica that is not
        primary renews nothing and takes nothing over.
        """

        while True:
            await asyncio.sleep(_PRIMARY_LOCK_HEARTBEAT_SECONDS)
            try:
                await state_store.renew_primary_lock(pool_name, self._owner_id, ttl)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "OpenSandbox client-pool lock renewal failed: pool=%s error=%s",
                    pool_name,
                    _error_detail(exc),
                )

    async def _stop_heartbeat(self, pool_name: str) -> None:
        task = self._heartbeats.pop(pool_name, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _creator(
        self,
        spec: SandboxClientPoolSpec,
        creator: SandboxClientPoolCreator,
    ) -> Any:
        async def create_unchecked(_context: Any) -> Sandbox:
            from astrabox.providers.open_sandbox.sandbox import (
                OpenSandboxHandle,
                OpenSandboxSandboxProvider,
            )

            provider = OpenSandboxSandboxProvider(transport=self._transport)
            member = _pool_member(spec)
            try:
                handle = await creator(member)
            except asyncio.CancelledError:
                # The lifecycle server can allocate the box before its create
                # response reaches the SDK. The SDK then has no id to clean up.
                # This invocation has not returned to the pool, so its exact
                # member cannot have been published or claimed.
                from astrabox.providers.open_sandbox.executor import (
                    destroy_open_sandbox_box,
                )

                candidate = await provider.find_sandbox_by_assignment(member.assignment_id)
                if candidate is not None and candidate.session_id == member.session_id:
                    await destroy_open_sandbox_box(provider, candidate.sandbox_id)
                raise
            if not isinstance(handle, OpenSandboxHandle):
                raise TypeError("OpenSandbox client-pool creator returned a foreign handle")
            return handle.sidecar_faces

        async def create(context: Any) -> Sandbox:
            try:
                return await create_unchecked(context)
            except asyncio.CancelledError:
                logger.warning(
                    "OpenSandbox client-pool member creation cancelled: pool=%s",
                    spec.pool_name,
                )
                raise
            except APIError as exc:
                logger.error(
                    "OpenSandbox client-pool member creation failed: pool=%s code=%s error=%s",
                    spec.pool_name,
                    exc.code,
                    _error_detail(exc),
                )
                raise
            except Exception as exc:
                logger.error(
                    "OpenSandbox client-pool member creation failed: "
                    "pool=%s error_type=%s error=%s",
                    spec.pool_name,
                    type(exc).__name__,
                    _error_detail(exc),
                )
                raise _pool_unavailable(
                    spec.pool_name,
                    "create a member",
                    exc,
                ) from exc

        return create

    def _preparer(
        self,
        spec: SandboxClientPoolSpec,
        preparer: SandboxClientPoolPreparer,
    ) -> Any:
        async def prepare(sdk_sandbox: Sandbox) -> None:
            from astrabox.providers.open_sandbox.sandbox import OpenSandboxHandle

            handle = OpenSandboxHandle(sdk_sandbox)
            try:
                async with asyncio.timeout(int(spec.preparation_timeout_seconds)):
                    await preparer(handle)
                logger.info(
                    "OpenSandbox client-pool member prepared: pool=%s sandbox=%s",
                    spec.pool_name,
                    handle.sandbox_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "OpenSandbox client-pool member preparation failed: "
                    "pool=%s sandbox=%s error_type=%s error=%s",
                    spec.pool_name,
                    str(getattr(sdk_sandbox, "id", "") or "<unknown>"),
                    type(exc).__name__,
                    _error_detail(exc),
                )
                raise

        return prepare

    async def acquire(
        self,
        spec: SandboxClientPoolSpec,
    ) -> Any | None:
        """Take one prepared member; only an honestly empty pool is a miss."""

        async with self._lock:
            pool = self._pools.get(spec.pool_name)
        if pool is None:
            exc = PoolNotRunningException("this event loop has not started the pool scheduler")
            raise _pool_unavailable(spec.pool_name, "acquire a member", exc)

        try:
            sdk_sandbox = await pool.acquire(
                sandbox_timeout=timedelta(
                    seconds=int(load_astrabox_settings().sandbox_lease_seconds)
                ),
                # DIRECT_CREATE bypasses the preparer. FAIL_FAST guarantees
                # every returned member crossed the platform callback first.
                policy=AcquirePolicy.FAIL_FAST,
            )
        except asyncio.CancelledError:
            raise
        except PoolEmptyException:
            logger.info(
                "OpenSandbox client pool is empty: pool=%s",
                spec.pool_name,
            )
            return None
        except _POOL_ACQUIRE_FAILURES as exc:
            raise _pool_unavailable(
                spec.pool_name,
                "acquire a prepared member",
                exc,
            ) from exc
        except Exception as exc:
            raise _pool_unavailable(
                spec.pool_name,
                "acquire a prepared member",
                exc,
            ) from exc

        from astrabox.providers.open_sandbox.sandbox import OpenSandboxHandle

        logger.info(
            "OpenSandbox client pool acquired: pool=%s sandbox=%s",
            spec.pool_name,
            sdk_sandbox.id,
        )
        return OpenSandboxHandle(sdk_sandbox)

    async def describe(self, pool_name: str) -> SandboxClientPoolStatus:
        """Read shared inventory without starting or changing the pool."""

        name = str(pool_name or "").strip()
        if not name:
            raise APIError(
                code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
                message="OpenSandbox client-pool status requires a pool name",
                status_code=503,
            )
        async with self._lock:
            pool = self._pools.get(name)
        if pool is None:
            state_store = await self._ensure_state_store()
            try:
                max_idle = await state_store.get_max_idle(name)
                counters = (
                    await state_store.snapshot_counters(name) if max_idle is not None else None
                )
                idle_entries = (
                    await state_store.snapshot_idle_entries(name)
                    if max_idle is not None
                    else []
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _pool_unavailable(
                    name,
                    "read its shared state",
                    exc,
                ) from exc
            if max_idle is not None and counters is not None:
                idle_count = int(getattr(counters, "idle_count", 0) or 0)
                return SandboxClientPoolStatus(
                    pool_name=name,
                    lifecycle_state=None,
                    ready=idle_count > 0,
                    idle_count=idle_count,
                    max_idle=int(max_idle),
                    # Reconcile health belongs to the process running the SDK
                    # scheduler; shared Redis only answers inventory.
                    failure_count=None,
                    backoff_active=None,
                    in_flight_operations=None,
                    last_error=None,
                    idle_sandbox_ids=tuple(
                        str(entry.sandbox_id) for entry in idle_entries
                    ),
                )
            return SandboxClientPoolStatus(
                pool_name=name,
                lifecycle_state="NOT_STARTED",
                ready=False,
                idle_count=0,
                max_idle=0,
                failure_count=0,
                backoff_active=False,
                in_flight_operations=0,
                last_error=False,
            )

        try:
            snapshot = await pool.snapshot()
            state_store = await self._ensure_state_store()
            idle_entries = await state_store.snapshot_idle_entries(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _pool_unavailable(name, "read its local state", exc) from exc
        lifecycle = str(getattr(getattr(snapshot, "lifecycle_state", None), "value", "") or "")
        idle_count = int(getattr(snapshot, "idle_count", 0) or 0)
        return SandboxClientPoolStatus(
            pool_name=name,
            lifecycle_state=lifecycle or None,
            ready=lifecycle == "RUNNING" and idle_count > 0,
            idle_count=idle_count,
            max_idle=int(getattr(snapshot, "max_idle", 0) or 0),
            failure_count=int(getattr(snapshot, "failure_count", 0) or 0),
            backoff_active=bool(getattr(snapshot, "backoff_active", False)),
            in_flight_operations=int(getattr(snapshot, "in_flight_operations", 0) or 0),
            last_error=bool(getattr(snapshot, "last_error", None)),
            idle_sandbox_ids=tuple(
                str(entry.sandbox_id) for entry in idle_entries
            ),
        )

    async def retire(self, pool_name: str) -> None:
        """Fence a namespace and destroy its idle members.

        A member whose create finished but never reached the idle inventory is
        not in the namespace; it expires with the pool's idle lease.
        """

        name = str(pool_name or "").strip()
        if not name:
            return
        async with self._lock:
            pool = self._pools.pop(name, None)
            self._sizes.pop(name, None)
        await self._stop_heartbeat(name)
        if pool is not None:
            try:
                await pool.shutdown(graceful=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _pool_unavailable(
                    name,
                    "stop its local scheduler",
                    exc,
                ) from exc

        state_store = await self._ensure_state_store()
        manager = SandboxPoolManagerAsync(
            state_store=state_store,
            connection_config=self._connection_config(),
        )
        try:
            result = await manager.destroy(
                name,
                options=PoolDestroyOptions(tombstone_ttl=_POOL_TOMBSTONE_TTL),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _pool_unavailable(name, "destroy its namespace", exc) from exc

        logger.info(
            "OpenSandbox client pool retired: pool=%s idle_killed=%s tombstone_s=%d",
            name,
            result.killed_idle_count,
            int(_POOL_TOMBSTONE_TTL.total_seconds()),
        )

    async def shutdown(self) -> None:
        """Stop local schedulers without deleting their shared namespaces.

        The SDK releases the primary lock before it waits for an in-flight
        reconcile tick, and a tick that had not yet taken the lock takes it
        again: observed when a restart left the lock held for its whole TTL.
        Its graceful shutdown returns only after that tick drained and no new
        tick can start, so the lock is released once more here, for this
        owner only, and the next process does not wait for it to expire.
        """

        async with self._lock:
            pools = dict(self._pools)
            self._pools.clear()
            self._sizes.clear()
        for name in pools:
            await self._stop_heartbeat(name)
        await asyncio.gather(
            *(pool.shutdown(graceful=True) for pool in pools.values()),
            return_exceptions=True,
        )
        if pools and self._state_store is not None:
            for name in pools:
                try:
                    await self._state_store.release_primary_lock(name, self._owner_id)
                except Exception as exc:
                    logger.warning(
                        "OpenSandbox client-pool lock release failed: pool=%s error=%s",
                        name,
                        _error_detail(exc),
                    )
        if self._redis is not None:
            close = getattr(self._redis, "aclose", None) or getattr(
                self._redis,
                "close",
                None,
            )
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result


__all__ = ["OpenSandboxClientPoolRegistry"]
