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
import os
import socket
import threading
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from opensandbox import Sandbox, SandboxManager, SandboxPoolAsync
from opensandbox.async_redis_pool_store import AsyncRedisPoolStateStore
from opensandbox.config import ConnectionConfig
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
from astrabox.providers.open_sandbox._metadata import client_pool_tag
from astrabox.providers.open_sandbox.execd_access import (
    execd_access_headers,
    execd_access_required,
    with_execd_access,
)
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

# Members prepared concurrently per pool. A refill of a large pool starts this
# many creates at once instead of all of them, so one Agent's refill cannot
# exhaust a single Docker host or node. The SDK creates at most the deficit, and
# a resize changes only the pool's size, so the limit is fixed rather than
# derived from the size the pool started with.
_WARMUP_CONCURRENCY_LIMIT = 8

# The replica that replenishes a pool holds the SDK's primary lock. The SDK
# renews it on its own heartbeat while this process holds it and fences
# in-flight warmups when a renewal fails, and warns unless the lock outlasts its
# warmup ready timeout, so the lock is that timeout plus this margin.
_PRIMARY_LOCK_TTL_MARGIN_SECONDS = 30

# How long a pool waits after a member fails before the next one, doubling with
# each consecutive failure: the kubelet's container restart back-off (10 s,
# doubling, at most five minutes). The supplier pool replaces a failed member on
# its next reconcile tick, about a second later, so without it a cause that
# persists creates and destroys a box for as long as it lasts.
_MEMBER_RETRY_FIRST_SECONDS = 10.0
_MEMBER_RETRY_MAX_SECONDS = 300.0

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


class _MemberSandbox(Sandbox):
    """How the SDK pool connects a member it hands out: with that member's execd credential.

    The pool connects an acquired member with the pool's own connection
    config, which carries no member's credential, and then checks the
    member's execd through it. A member whose execd requires one would answer
    that check 401 and be discarded. The pool's ``sandbox_factory`` is the
    SDK's place to change how a member is connected; this one reads the
    member's credential from its metadata (:mod:`.execd_access`), as
    :meth:`OpenSandboxSandboxProvider.connect` does.
    """

    #: The HTTP transport of the registry that made this factory
    #: (:func:`_member_sandbox_factory`); None is the default one.
    transport: Any = None

    @classmethod
    async def connect(
        cls,
        sandbox_id: str,
        connection_config: ConnectionConfig | None = None,
        health_check: Callable[[Sandbox], Awaitable[bool]] | None = None,
        connect_timeout: timedelta = timedelta(seconds=30),
        health_check_polling_interval: timedelta = timedelta(milliseconds=200),
        skip_health_check: bool = False,
    ) -> Sandbox:
        config = connection_config or ConnectionConfig()
        manager = await SandboxManager.create(connection_config=config)
        try:
            info = await manager.get_sandbox_info(str(sandbox_id))
        finally:
            await manager.close()
        sandbox = await super().connect(
            sandbox_id,
            connection_config=with_execd_access(
                config, execd_access_headers(getattr(info, "metadata", None))
            ),
            health_check=health_check,
            connect_timeout=connect_timeout,
            health_check_polling_interval=health_check_polling_interval,
            skip_health_check=skip_health_check,
        )
        if execd_access_required(load_astrabox_settings()):
            # Every prepared member is handed out through here, so a member that
            # does not hold its execd to its own credential is refused here,
            # whatever made it: one prepared before the deployment required the
            # credential, or one whose create check never finished. The pool
            # removes and destroys a member this refuses and tries the next.
            from astrabox.providers.open_sandbox.executor import (
                require_open_sandbox_execd_access,
            )
            from astrabox.providers.open_sandbox.sandbox import (
                OpenSandboxHandle,
                OpenSandboxSandboxProvider,
            )

            try:
                await require_open_sandbox_execd_access(
                    OpenSandboxSandboxProvider(transport=cls.transport),
                    OpenSandboxHandle(sandbox),
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    await sandbox.close()
                raise
        return sandbox


def _member_sandbox_factory(transport: Any) -> type[_MemberSandbox]:
    """The member factory of one registry, with its HTTP transport."""

    return type("_MemberSandbox", (_MemberSandbox,), {"transport": transport})


def _client_pool_session_id(pool_name: str) -> str:
    """Return a unique synthetic session id valid as a Kubernetes label value."""

    return f"client-pool-{client_pool_tag(pool_name)}-{uuid4().hex[:24]}"


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
        self._lock = asyncio.Lock()
        # Per pool: why its last member failed, how many failed in a row, and
        # the event that ends a back-off when the pool is retired or stopped.
        self._member_failures: dict[str, str] = {}
        self._failure_streaks: dict[str, int] = {}
        self._closing: dict[str, asyncio.Event] = {}
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
                warmup_ready_timeout=timedelta(seconds=int(settings.sandbox_ready_timeout_seconds)),
                warmup_skip_health_check=True,
                warmup_sandbox_preparer=self._preparer(spec, preparer),
                idle_timeout=timedelta(seconds=int(spec.idle_timeout_seconds)),
                drain_timeout=timedelta(seconds=30),
                sandbox_creator=self._creator(spec, creator),
                sandbox_factory=_member_sandbox_factory(self._transport),
            )
            self._closing[spec.pool_name] = asyncio.Event()
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

        logger.info(
            "OpenSandbox client-pool scheduler started: pool=%s",
            spec.pool_name,
        )

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
                sandbox = await create_unchecked(context)
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
                await self._member_failed(spec.pool_name, exc)
                raise
            except Exception as exc:
                logger.error(
                    "OpenSandbox client-pool member creation failed: "
                    "pool=%s error_type=%s error=%s",
                    spec.pool_name,
                    type(exc).__name__,
                    _error_detail(exc),
                )
                failure = _pool_unavailable(spec.pool_name, "create a member", exc)
                await self._member_failed(spec.pool_name, failure)
                raise failure from exc
            return sandbox

        return create

    async def _member_failed(self, pool_name: str, failure: APIError) -> None:
        """Record why a member failed, then hold the pool's next attempt back.

        Waiting here, before the failure reaches the supplier pool, keeps this
        warmup in flight, so the pool starts no other until it ends. Each
        consecutive failure doubles the wait (:data:`_MEMBER_RETRY_FIRST_SECONDS`
        up to :data:`_MEMBER_RETRY_MAX_SECONDS`); a member that succeeds resets
        it, and retiring or stopping the pool ends it.
        """
        self._member_failures[pool_name] = f"{failure.code}: {failure.message}"
        streak = self._failure_streaks.get(pool_name, 0) + 1
        self._failure_streaks[pool_name] = streak
        closing = self._closing.get(pool_name)
        if closing is None:
            return
        delay = min(_MEMBER_RETRY_FIRST_SECONDS * 2 ** (streak - 1), _MEMBER_RETRY_MAX_SECONDS)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(closing.wait(), timeout=delay)

    def _member_succeeded(self, pool_name: str) -> None:
        self._member_failures.pop(pool_name, None)
        self._failure_streaks.pop(pool_name, None)

    def _end_backoff(self, pool_name: str) -> None:
        """End a stopping pool's back-off, so its in-flight warmup does not hold the stop."""
        closing = self._closing.pop(pool_name, None)
        if closing is not None:
            closing.set()
        self._member_failures.pop(pool_name, None)
        self._failure_streaks.pop(pool_name, None)

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
                await self._member_failed(
                    spec.pool_name,
                    exc
                    if isinstance(exc, APIError)
                    else _pool_unavailable(spec.pool_name, "prepare a member", exc),
                )
                raise
            self._member_succeeded(spec.pool_name)

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
                last_error=None,
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
            last_error=self._member_failures.get(name),
            idle_sandbox_ids=tuple(
                str(entry.sandbox_id) for entry in idle_entries
            ),
        )

    async def retire(self, pool_name: str) -> None:
        """Fence a namespace and destroy its idle members.

        Returns once the SDK has fenced the namespace and drained its idle
        inventory, and raises when it could not, so the platform keeps the pool
        recorded and retries. The SDK removes a member's id from shared state
        before destroying the member and only logs a destruction that fails;
        a later call finds the namespace destroyed and reaches no member. That
        member, a member whose create finished after the fence, and one whose
        process took it from the inventory and exited before destroying it are
        outside the namespace. The ownerless reap destroys each of them once
        the platform stops recording the pool
        (:meth:`~.sandbox.OpenSandboxSandboxProvider.unclaimed_sandbox_refusal`).
        """

        name = str(pool_name or "").strip()
        if not name:
            return
        async with self._lock:
            pool = self._pools.pop(name, None)
            self._sizes.pop(name, None)
            self._end_backoff(name)
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

        unconfirmed = result.drained_idle_count - result.killed_idle_count
        if unconfirmed:
            logger.warning(
                "OpenSandbox client pool retired with idle members whose destruction "
                "failed: pool=%s unconfirmed=%d (opensandbox.pool_manager logs each "
                "sandbox_id); the ownerless reap destroys them once the pool is no "
                "longer recorded",
                name,
                unconfirmed,
            )
        logger.info(
            "OpenSandbox client pool retired: pool=%s idle_drained=%s idle_killed=%s "
            "tombstone_s=%d",
            name,
            result.drained_idle_count,
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
            for name in list(self._closing):
                self._end_backoff(name)
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
