"""PostgreSQL runtime advisory locks and transactional-notice listening."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast, get_args

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.exc import DBAPIError

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.providers.postgres.engine import get_engine, redacted_url
from astrabox.seams.persistence_coordination import NoticeKind, RuntimeLockCheck, SessionNotice

logger = get_logger(__name__)
_RECONNECT_DELAY_S = 1.0
_CHANNEL = "astrabox_session"


def _parse_notice(payload: str) -> SessionNotice | None:
    kind, _, rest = payload.partition(":")
    seq_text, _, session_id = rest.partition(":")
    if kind not in get_args(NoticeKind) or not session_id or not seq_text.isdigit():
        return None
    return SessionNotice(session_id=session_id, kind=cast(NoticeKind, kind), seq=int(seq_text))


class PostgresCoordination:
    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    async def notify(self, connection: AsyncConnection, notice: SessionNotice) -> None:
        """Enqueue native NOTIFY inside the shared recipe's transaction."""
        synchronous = connection.sync_connection
        if synchronous is None or not connection.in_transaction() or (
            synchronous.get_execution_options().get("isolation_level") == "AUTOCOMMIT"
        ):
            raise ValueError("transactional notification requires an active database transaction")
        payload = f"{notice.kind}:{notice.seq}:{notice.session_id}"
        await connection.execute(select(func.pg_notify(_CHANNEL, payload)))

    @asynccontextmanager
    async def runtime_lock(self, session_id: str) -> AsyncIterator[RuntimeLockCheck]:
        """Hold the Session's transaction advisory lock on a dedicated connection.

        Detected connection loss cancels the owner and refuses success. The
        supplier may already have accepted an in-flight request. Repository
        writes use their own pool while this connection retains the lock.
        """
        async with get_engine(self._database_url, runtime_lock=True).begin() as connection:
            driver: Any = (await connection.get_raw_connection()).driver_connection
            owner = asyncio.current_task()
            assert owner is not None
            active = True

            def disconnected(_connection: Any) -> None:
                if active:
                    owner.cancel()

            async def check() -> None:
                if driver.is_closed():
                    raise APIError(
                        code="SESSION_RUNTIME_LOCK_LOST",
                        message="runtime coordination connection was lost; supplier work may be incomplete",
                        status_code=502,
                    )
                await connection.execute(select(1))

            driver.add_termination_listener(disconnected)
            try:
                await connection.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(f"astrabox.session.runtime:{session_id}", 0)
                        )
                    )
                )
                yield check
                await check()
            except (asyncio.CancelledError, DBAPIError):
                if driver.is_closed():
                    await check()
                raise
            finally:
                active = False
                driver.remove_termination_listener(disconnected)

    async def listen(
        self,
        *,
        on_notice: Callable[[SessionNotice], None],
        on_resync: Callable[[], None],
        on_ready: Callable[[], None],
    ) -> None:
        """Restore LISTEN after disconnection and report its missing-notice gap."""
        dsn = self._database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

        def received(_connection: Any, _pid: int, _channel: str, payload: str) -> None:
            parsed = _parse_notice(payload)
            if parsed is None:
                logger.error("session feed ignored a malformed notice %r", payload)
                return
            on_notice(parsed)

        first = True
        while True:
            connection = None
            try:
                connection = await asyncpg.connect(dsn, server_settings={"application_name": "astrabox-feed"})
                lost = asyncio.Event()
                connection.add_termination_listener(lambda _conn: lost.set())
                await connection.add_listener(_CHANNEL, received)
                if not first:
                    on_resync()
                first = False
                on_ready()
                await lost.wait()
                logger.warning("session feed connection closed; listening again")
                continue
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
                if isinstance(exc, asyncpg.InterfaceError) and (
                    connection is None or not connection.is_closed()
                ):
                    raise
                logger.error("session feed cannot listen on %s: %s", redacted_url(dsn), exc)
            finally:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await connection.close()
            await asyncio.sleep(_RECONNECT_DELAY_S)
