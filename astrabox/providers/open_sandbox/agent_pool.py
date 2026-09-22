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
from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

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

# One deterministic assignment identifies one unpublished create. Supporting
# more than one idle member first requires distinct durable ordinals through
# the entire recovery path; refuse rather than reuse an assignment.
_POOL_MAX_IDLE = 1

# A retired name is briefly fenced so a replica with stale local state cannot
# recreate the namespace while its peer is destroying it. Pool names are
# deterministic and may be selected again, so the vendor's seven-day default
# would be too long here.
_POOL_TOMBSTONE_TTL = timedelta(minutes=5)
_RECONCILE_SECONDS = 30

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


def _require_supported_spec(spec: SandboxClientPoolSpec) -> None:
    if int(spec.max_idle) == _POOL_MAX_IDLE:
        return
    raise APIError(
        code="SANDBOX_CLIENT_POOL_UNSUPPORTED",
        message=(
            "OpenSandbox client pools currently support exactly one idle "
            f"member; pool {spec.pool_name!r} requested {spec.max_idle}"
        ),
        status_code=501,
    )


def _client_pool_session_id(pool_name: str) -> str:
    """Return a unique synthetic session id valid as a Kubernetes label value."""

    pool_hash = hashlib.sha256(pool_name.encode("utf-8")).hexdigest()[:12]
    return f"client-pool-{pool_hash}-{uuid4().hex[:24]}"


def _client_pool_assignment_id(pool_name: str) -> str:
    """Return the durable identity of the official pool's one warm slot."""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"astrabox:open-sandbox-client-pool:{pool_name}:idle-0",
        )
    )


def _client_pool_owns_session(pool_name: str, session_id: str | None) -> bool:
    pool_hash = hashlib.sha256(pool_name.encode("utf-8")).hexdigest()[:12]
    return str(session_id or "").startswith(f"client-pool-{pool_hash}-")


def _pool_member(spec: SandboxClientPoolSpec) -> SandboxClientPoolMember:
    return SandboxClientPoolMember(
        pool_name=spec.pool_name,
        member_index=0,
        session_id=_client_pool_session_id(spec.pool_name),
        assignment_id=_client_pool_assignment_id(spec.pool_name),
    )


def _assignment_release_settle_seconds() -> int:
    settings = load_astrabox_settings()
    ready_timeout = int(getattr(settings, "sandbox_ready_timeout_seconds", 30))
    request_timeout = int(getattr(settings, "sandbox_request_timeout_seconds", 15))
    # The claim handoff spans one ready wait plus renew, assignment patch, and
    # failure cleanup requests; the pool assignment remains authoritative
    # throughout that bounded window.
    return max(1, ready_timeout + request_timeout * 3 + 1)


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
        self._lock = asyncio.Lock()

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
        """Start or join the official pool without waiting for its first member."""

        _require_supported_spec(spec)
        async with self._lock:
            if spec.pool_name in self._pools:
                return
            state_store = await self._ensure_state_store()
            settings = load_astrabox_settings()
            pool = self._pool_factory(
                pool_name=spec.pool_name,
                max_idle=spec.max_idle,
                state_store=state_store,
                connection_config=self._connection_config(),
                # The SDK requires this even when the custom creator below
                # owns the actual create operation.
                creation_spec=PoolCreationSpec(image=spec.creation_image),
                warmup_concurrency=1,
                primary_lock_ttl=timedelta(
                    seconds=(
                        int(settings.sandbox_ready_timeout_seconds)
                        + int(spec.preparation_timeout_seconds)
                        + 60
                    )
                ),
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

        logger.info(
            "OpenSandbox client-pool scheduler started: pool=%s",
            spec.pool_name,
        )

    async def _reclaim_unpublished_member(
        self,
        spec: SandboxClientPoolSpec,
        provider: Any,
    ) -> None:
        """Remove a create that never reached the SDK's idle inventory."""

        from astrabox.providers.open_sandbox.executor import (
            destroy_open_sandbox_box,
        )

        assignment = _client_pool_assignment_id(spec.pool_name)
        candidate = await provider.find_sandbox_by_assignment(assignment)
        if candidate is None:
            return
        if not _client_pool_owns_session(spec.pool_name, candidate.session_id):
            raise APIError(
                code="SANDBOX_ASSIGNMENT_CONFLICT",
                message=(
                    f"client-pool assignment {assignment!r} belongs to session "
                    f"{candidate.session_id!r}, not pool {spec.pool_name!r}"
                ),
                status_code=409,
            )

        state_store = await self._ensure_state_store()

        async def is_official_idle(sandbox_id: str) -> bool:
            entries = await state_store.snapshot_idle_entries(spec.pool_name)
            return any(
                str(getattr(entry, "sandbox_id", "") or "") == sandbox_id for entry in entries
            )

        if await is_official_idle(candidate.sandbox_id):
            raise APIError(
                code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
                message=(
                    f"OpenSandbox client pool {spec.pool_name!r} requested "
                    "replenishment while its durable member remains idle"
                ),
                status_code=503,
            )

        # The member may have just been popped by acquire(). Wait through the
        # bounded renew/adopt/cleanup window. A completed claim replaces this
        # assignment; a create whose publication was lost leaves it unchanged.
        deadline = asyncio.get_running_loop().time() + _assignment_release_settle_seconds()
        while True:
            settled = await provider.find_sandbox_by_assignment(assignment)
            if settled is None:
                return
            if settled.sandbox_id != candidate.sandbox_id:
                raise APIError(
                    code="SANDBOX_ASSIGNMENT_CONFLICT",
                    message=(
                        f"client-pool assignment {assignment!r} changed from "
                        f"sandbox {candidate.sandbox_id!r} to "
                        f"{settled.sandbox_id!r} while ownership was settling"
                    ),
                    status_code=409,
                )
            if await is_official_idle(settled.sandbox_id):
                raise APIError(
                    code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
                    message=(
                        f"OpenSandbox client pool {spec.pool_name!r} published "
                        "its durable member during reconciliation"
                    ),
                    status_code=503,
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(1.0, remaining))

        destruction = await destroy_open_sandbox_box(
            provider,
            settled.sandbox_id,
        )
        if not destruction.confirmed:
            raise APIError(
                code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
                message=(
                    "OpenSandbox could not reclaim unpublished client-pool "
                    f"member {settled.sandbox_id!r}: {destruction.detail}"
                ),
                status_code=502,
                data={
                    "sandbox_id": settled.sandbox_id,
                    "leaked_sandbox_id": settled.sandbox_id,
                },
            )
        logger.warning(
            "OpenSandbox client pool reclaimed unpublished member: "
            "pool=%s assignment=%s sandbox=%s",
            spec.pool_name,
            assignment,
            settled.sandbox_id,
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
            await self._reclaim_unpublished_member(spec, provider)
            handle = await creator(_pool_member(spec))
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

        _require_supported_spec(spec)
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
                max_idle=_POOL_MAX_IDLE,
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
        """Fence a namespace and destroy idle plus unpublished members."""

        name = str(pool_name or "").strip()
        if not name:
            return
        async with self._lock:
            pool = self._pools.pop(name, None)
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

        # manager.destroy() drains published idle entries. A create can finish
        # before publication, so use the durable assignment to remove that
        # additional orphan as the historical implementation did.
        from astrabox.providers.open_sandbox.executor import (
            destroy_open_sandbox_box,
        )
        from astrabox.providers.open_sandbox.sandbox import (
            OpenSandboxSandboxProvider,
        )

        provider = OpenSandboxSandboxProvider(transport=self._transport)
        assignment = _client_pool_assignment_id(name)
        try:
            unpublished = await provider.find_sandbox_by_assignment(assignment)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _pool_unavailable(
                name,
                "find an unpublished member after retirement",
                exc,
            ) from exc

        unpublished_id = ""
        if unpublished is not None:
            if not _client_pool_owns_session(name, unpublished.session_id):
                raise APIError(
                    code="SANDBOX_ASSIGNMENT_CONFLICT",
                    message=(
                        f"client-pool assignment {assignment!r} belongs to session "
                        f"{unpublished.session_id!r}, not pool {name!r}"
                    ),
                    status_code=409,
                )

            # acquire() removes the member from Redis before the platform can
            # replace its technical assignment with the claiming Agent
            # runtime's.
            # Namespace retirement must therefore wait through that bounded
            # hand-off before treating a remaining technical assignment as an
            # unpublished create. Otherwise an Agent edit can destroy a box
            # already promised to a user request.
            candidate_id = unpublished.sandbox_id
            deadline = (
                asyncio.get_running_loop().time()
                + _assignment_release_settle_seconds()
            )
            while True:
                settled = await provider.find_sandbox_by_assignment(assignment)
                if settled is None:
                    break
                if settled.sandbox_id != candidate_id:
                    raise APIError(
                        code="SANDBOX_ASSIGNMENT_CONFLICT",
                        message=(
                            f"client-pool assignment {assignment!r} changed from "
                            f"sandbox {candidate_id!r} to {settled.sandbox_id!r} "
                            "while retirement ownership was settling"
                        ),
                        status_code=409,
                    )
                if not _client_pool_owns_session(name, settled.session_id):
                    raise APIError(
                        code="SANDBOX_ASSIGNMENT_CONFLICT",
                        message=(
                            f"client-pool assignment {assignment!r} changed owner "
                            f"to session {settled.session_id!r} while retirement "
                            "ownership was settling"
                        ),
                        status_code=409,
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    unpublished = settled
                    break
                await asyncio.sleep(min(1.0, remaining))
            if settled is None:
                unpublished = None

        if unpublished is not None:
            unpublished_id = unpublished.sandbox_id
            destruction = await destroy_open_sandbox_box(provider, unpublished_id)
            if not destruction.confirmed:
                raise APIError(
                    code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
                    message=(
                        f"OpenSandbox retired client pool {name!r}, but its "
                        f"unpublished member {unpublished_id!r} survives: "
                        f"{destruction.detail}"
                    ),
                    status_code=502,
                    data={
                        "sandbox_id": unpublished_id,
                        "leaked_sandbox_id": unpublished_id,
                    },
                )
        logger.info(
            "OpenSandbox client pool retired: pool=%s idle_killed=%s "
            "unpublished_killed=%s tombstone_s=%d",
            name,
            result.killed_idle_count,
            bool(unpublished_id),
            int(_POOL_TOMBSTONE_TTL.total_seconds()),
        )

    async def shutdown(self) -> None:
        """Stop local schedulers without deleting their shared namespaces."""

        async with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        await asyncio.gather(
            *(pool.shutdown(graceful=True) for pool in pools),
            return_exceptions=True,
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
