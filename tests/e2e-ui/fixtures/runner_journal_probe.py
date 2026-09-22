"""Observe real journal compaction and read its retained wire without new input.

The existing gap-rebuild fault owns this whole-box runner restart. Observation
does not replace compaction, fabricate frames, acknowledge persistence, or
write SessionStore. The probe runs only after the host runtime was evicted.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


def run_observed(image_path: str, threshold: int, evidence_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("astrabox_e2e_gap_runner", image_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    runner.EnvelopeSender._JOURNAL_COMPACTION_THRESHOLD = threshold
    requirements: dict[str, Any] = {}
    original_open = runner.RunnerWsServer._open_session
    original_compact = runner.EnvelopeSender._compact_result_prefix_locked

    async def observe_open(server: Any, opening: Any, link: Any, ws: Any) -> Any:
        requirements.clear()
        requirements.update(deepcopy(opening["engine_requirements"]))
        return await original_open(server, opening, link, ws)

    def observe_compact(sender: Any) -> int:
        before = deepcopy(list(sender._journal))
        removed = original_compact(sender)
        if removed:
            checkpoint = sender.history_checkpoint
            with evidence_path.open("a", encoding="utf-8") as evidence:
                evidence.write(json.dumps({
                    "session_id": sender._session_id,
                    "engine_requirements": requirements,
                    "result_sequence": checkpoint.live.value,
                    "store_sequence": checkpoint.store.value,
                    "last_sequence": sender.last_seq,
                    "removed": removed,
                    "before": before,
                    "after": list(sender._journal),
                }) + "\n")
        return removed

    runner.RunnerWsServer._open_session = observe_open
    runner.EnvelopeSender._compact_result_prefix_locked = observe_compact
    asyncio.run(runner.main())


async def probe(port: int, session_id: str, evidence_path: Path) -> None:
    from websockets.asyncio.client import connect

    observations = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    if not observations or any(row["session_id"] != session_id for row in observations):
        raise RuntimeError("compaction evidence must belong to the requested Session")
    last = observations[-1]

    async def read(after_sequence: int) -> dict[str, Any]:
        async with asyncio.timeout(10), connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({
                "op": "attach", "session_id": session_id,
                "last_seen_seq": after_sequence,
                "engine_requirements": last["engine_requirements"],
            }))
            # _handle dispatches this read only after _open_session finishes
            # replay. Its response bounds the whole replay, including duplicate
            # frames after the advertised last sequence; no quiet-time guess.
            await ws.send(json.dumps({"op": "get_init_info"}))
            hello = json.loads(await ws.recv())
            if hello.get("op") != "hello" or hello.get("session_id") != session_id:
                raise RuntimeError(f"journal attach failed: {hello!r}")
            frames = []
            while True:
                frame = json.loads(await ws.recv())
                if frame.get("op") == "error":
                    raise RuntimeError(f"journal replay failed: {frame!r}")
                if frame.get("op") == "init_info":
                    return {"after_sequence": after_sequence, "hello": hello, "frames": frames}
                frames.append(frame)

    cold = await read(0)
    terminal = await read(last["result_sequence"] - 1)
    print(json.dumps({"observations": observations, "cold": cold, "terminal": terminal}))


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run_observed(sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]))
    elif sys.argv[1] == "probe":
        asyncio.run(probe(int(sys.argv[2]), sys.argv[3], Path(sys.argv[4])))
    else:
        raise ValueError("expected run or probe")
