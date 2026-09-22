"""The connection pool is fixed, bounded, and says who is holding it.

Both halves pin a failure that happened after five healthy hours: the server
logged ``Exception terminating connection …
CancelledError: Cancelled via cancel scope … by <Task
RequestResponseCycle.run_asgi()>`` — the pool closing an overflow connection
inside a request task whose client had disconnected — and from that second on
EVERY persistence op waited 30 s and failed with ``QueuePool limit of size 5
overflow 10 reached``, while SQLite itself handed a bare writer the lock in
under a millisecond. Two lessons, one test file:

* Nobody chose 5 + 10. It came from calling ``create_async_engine`` with no
  pool arguments. Overflow is what creates and destroys connections under load,
  and destroying one on the return path is what put a close inside a cancelled
  task.
* Nothing in the process could say which coroutines held the connections. Five
  hours of external forensics — thread dumps, fd tables, py-spy, pystack —
  could not answer it either, because a parked holder appears on no thread
  stack. Only the pool's own checkout events can.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from astrabox.persistence.repository.sqlite import engine as engine_mod


class _PoolTestCase(unittest.IsolatedAsyncioTestCase):
    """Engines are cached per URL, so each test gets its own file and disposes.

    Without the dispose, an engine's aiosqlite threads outlive the test's event
    loop and the next test finds connections bound to a closed one.
    """

    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._urls: list[tuple[str, str]] = []

    async def asyncTearDown(self) -> None:
        for key in self._urls:
            engine = engine_mod._ENGINES.pop(key, None)
            engine_mod._SESSIONMAKERS.pop(key, None)
            if engine is not None:
                await engine.dispose()

    def _engine(self, name: str, mode: str):
        url = f"sqlite+aiosqlite:///{Path(self._dir.name) / name}"
        engine = engine_mod.get_engine(url, mode=mode)
        self._urls.append((engine_mod.resolve_sqlite_url(url), mode))
        return engine


class PoolShapeTests(_PoolTestCase):
    async def test_file_backed_pool_is_fixed_with_no_overflow(self) -> None:
        for mode in ("write", "read"):
            with self.subTest(mode):
                engine = self._engine(f"{mode}.sqlite", mode)
                self.assertEqual(engine.pool.size(), engine_mod._POOL_SIZE[mode])
                self.assertEqual(
                    engine.pool._max_overflow,  # type: ignore[attr-defined]
                    0,
                    "an overflow connection is closed on return, inside the returning task",
                )

    def test_postgresql_preserves_its_ceiling_without_overflow(self) -> None:
        kwargs = engine_mod._pool_kwargs(
            "postgresql+asyncpg://astrabox:secret@postgres/astrabox", "read"
        )
        self.assertEqual(kwargs["pool_size"], 30)
        self.assertEqual(
            kwargs["max_overflow"],
            0,
            "a cancelled request must never own an overflow connection's close",
        )

    async def test_a_shortage_fails_well_before_the_inherited_half_minute(self) -> None:
        engine = self._engine("timeout.sqlite", "write")
        self.assertLessEqual(
            engine.pool._timeout,  # type: ignore[attr-defined]
            15.0,
            "a full pool must surface as an error, not as a 30 s hang per request",
        )

    async def test_memory_urls_take_no_pool_arguments(self) -> None:
        # ``:memory:`` resolves to a single-connection pool that REJECTS sizing
        # arguments outright, so the shape has to follow the URL.
        self.assertEqual(
            engine_mod._pool_kwargs("sqlite+aiosqlite:///:memory:", "read"), {}
        )
        engine = engine_mod.get_engine("sqlite+aiosqlite:///:memory:", mode="read")
        async with engine.connect() as conn:
            self.assertEqual((await conn.exec_driver_sql("select 1")).scalar(), 1)


class PoolReportTests(_PoolTestCase):
    async def test_a_held_connection_names_its_holder(self) -> None:
        engine = self._engine("holder.sqlite", "read")
        released = asyncio.Event()
        reported: list[str] = []

        async def holder_coroutine() -> None:
            async with engine.connect() as conn:
                await conn.exec_driver_sql("select 1")
                reported.append(engine_mod.pool_report())
                released.set()

        task = asyncio.get_running_loop().create_task(holder_coroutine())
        await released.wait()
        await task

        self.assertTrue(reported, "the report must be reachable while a connection is held")
        line = reported[0]
        self.assertIn(
            "holder_coroutine",
            line,
            f"the report must name the coroutine holding the connection: {line}",
        )
        self.assertIn("read(size=24 idle=0 out=1)", line, line)

        # And it must not keep claiming a holder after checkin, or the next
        # incident reads as a leak that already resolved itself.
        self.assertNotIn("holder_coroutine", engine_mod.pool_report())


if __name__ == "__main__":
    unittest.main()
