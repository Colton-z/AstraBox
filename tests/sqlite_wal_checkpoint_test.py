"""Commits never do checkpoint work; the checkpointer owns the WAL.

SQLite's automatic checkpoint runs inside the commit that crosses its page
threshold. Under continuous readers, that commit can inherit the accumulated
checkpoint backlog while holding the write lock. Disabling automatic
checkpoints keeps that work in the bounded background checkpointer.
"""

from __future__ import annotations

import asyncio
import unittest

from astrabox.persistence.repository.sqlite import engine as engine_mod


class ConnectionPragmaTests(unittest.IsolatedAsyncioTestCase):
    async def test_connections_disable_in_commit_checkpoints(self) -> None:
        url = "sqlite+aiosqlite:///:memory:"
        eng = engine_mod.get_engine(url, mode="write")
        async with eng.connect() as conn:
            auto = (await conn.exec_driver_sql("PRAGMA wal_autocheckpoint")).scalar()
            limit = (await conn.exec_driver_sql("PRAGMA journal_size_limit")).scalar()
        self.assertEqual(int(auto or 0), 0, "a commit must never pay for a checkpoint")
        self.assertEqual(
            int(limit or -1),
            engine_mod._JOURNAL_SIZE_LIMIT_BYTES,
            "a restart must shrink the file back down",
        )


class CheckpointLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_loop_survives_a_failing_pass(self) -> None:
        # A dead checkpointer recreates the unbounded-WAL failure it exists to
        # end, so an erroring pass logs and backs off instead of exiting.
        real_sleep = asyncio.sleep
        calls = {"n": 0}

        def _boom(_path: str, _mode: str):
            calls["n"] += 1
            raise RuntimeError("checkpoint unavailable")

        async def fast_sleep(_delay: float) -> None:
            await real_sleep(0)

        orig_run, orig_sleep = engine_mod._run_checkpoint, asyncio.sleep
        engine_mod._run_checkpoint = _boom  # type: ignore[assignment]
        asyncio.sleep = fast_sleep  # type: ignore[assignment]
        try:
            task = asyncio.get_running_loop().create_task(
                engine_mod.checkpoint_loop("sqlite+aiosqlite:///:memory:")
            )
            for _ in range(200):
                await real_sleep(0)
                if calls["n"] >= 2:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            engine_mod._run_checkpoint = orig_run  # type: ignore[assignment]
            asyncio.sleep = orig_sleep  # type: ignore[assignment]
        self.assertGreaterEqual(calls["n"], 2, "the loop must outlive a failing pass")


if __name__ == "__main__":
    unittest.main()
