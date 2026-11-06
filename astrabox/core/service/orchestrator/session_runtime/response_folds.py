"""Each response's folded UI message, built once per stored output position.

A response can stream tens of thousands of parts, and reading, decoding and
folding them is most of what a history read costs. The output log is
append-only and numbered in order (see ``SessionOutput``): the parts of a
response up to its last part at or before a position never change. So the
fold of a response is determined by its ``(session, response, last seq)``,
and is kept under that key in this process. A part stored later moves the
response's last ``seq`` and folds it again; a page read at an older
position sees an older last ``seq`` and folds what it covers.

Only the fold is kept. What history lays over it on every read, the
response row's status and outcome and the interactions' answers, is read
fresh each time. Every read asks the database for each response's last
``seq``, so another process's writes are seen and a database error fails
the read. Concurrent reads of the same fold share one build.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from astrabox.core.service.orchestrator.session_runtime.ui_message import fold_message
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository

#: Serialized size of the folds kept in one process; the least recently read go first.
_KEPT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class _Kept:
    last_seq: int
    message: dict[str, Any]
    size: int


class ResponseFolds:
    def __init__(
        self,
        *,
        facts: SessionRuntimeRepository,
        reads: SessionReadRepository,
        kept_bytes: int = _KEPT_BYTES,
    ) -> None:
        self._facts = facts
        self._reads = reads
        self._kept_bytes = kept_bytes
        self._kept: OrderedDict[tuple[str, str], _Kept] = OrderedDict()
        self._size = 0
        self._building: dict[tuple[str, str, int], asyncio.Task[dict[str, dict[str, Any]]]] = {}

    async def messages(
        self, session_id: str, responses: Sequence[Mapping[str, Any]], *, through_seq: int | None
    ) -> dict[str, dict[str, Any]]:
        """Each response's message folded from its parts through ``through_seq`` (all for ``None``).

        The messages are the caller's to change.
        """
        folded: dict[str, dict[str, Any]] = {}
        logged: list[str] = []
        for row in responses:
            response_id = str(row["response_id"])
            record = row.get("import_record")
            if record:
                # An imported reply's parts were never output; they live on its row.
                folded[response_id] = fold_message(response_id, record["parts"])
            else:
                logged.append(response_id)
        last_seqs = await self._reads.last_output_seqs(session_id, logged, through_seq=through_seq)
        waiting: dict[str, asyncio.Task[dict[str, dict[str, Any]]]] = {}
        unbuilt: dict[str, int] = {}
        for response_id in logged:
            last_seq = last_seqs.get(response_id)
            if last_seq is None:
                folded[response_id] = fold_message(response_id, ())
                continue
            kept = self._kept.get((session_id, response_id))
            if kept is not None and kept.last_seq == last_seq:
                self._kept.move_to_end((session_id, response_id))
                folded[response_id] = deepcopy(kept.message)
                continue
            building = self._building.get((session_id, response_id, last_seq))
            if building is not None:
                waiting[response_id] = building
            else:
                unbuilt[response_id] = last_seq
        if unbuilt:
            # Its own task, so a reader that goes away does not take the
            # build from the others waiting on it.
            task = asyncio.create_task(self._build(session_id, unbuilt))
            keys = [(session_id, response_id, last_seq) for response_id, last_seq in unbuilt.items()]
            for key in keys:
                self._building[key] = task
            task.add_done_callback(lambda done: self._built(keys, done))
            waiting.update(dict.fromkeys(unbuilt, task))
        for response_id, building in waiting.items():
            built = await asyncio.shield(building)
            folded[response_id] = deepcopy(built[response_id])
        return folded

    async def _build(self, session_id: str, last_seqs: Mapping[str, int]) -> dict[str, dict[str, Any]]:
        rows = await self._facts.output_for_responses(
            session_id, list(last_seqs), through_seq=max(last_seqs.values())
        )
        parts: dict[str, list[dict[str, Any]]] = {response_id: [] for response_id in last_seqs}
        for row in rows:
            response_id = str(row["response_id"])
            # The bound is the highest of these responses' last parts; a
            # response that has stored more since stops at its own.
            if row["seq"] <= last_seqs[response_id]:
                parts[response_id].append(row["part"])
        built = {response_id: fold_message(response_id, chunks) for response_id, chunks in parts.items()}
        for response_id, message in built.items():
            self._keep((session_id, response_id), _Kept(last_seqs[response_id], message, len(json.dumps(message))))
        return built

    def _built(self, keys: Sequence[tuple[str, str, int]], done: asyncio.Task[Any]) -> None:
        for key in keys:
            if self._building.get(key) is done:
                del self._building[key]

    def _keep(self, key: tuple[str, str], kept: _Kept) -> None:
        previous = self._kept.pop(key, None)
        if previous is not None:
            self._size -= previous.size
        if kept.size > self._kept_bytes:
            return
        self._kept[key] = kept
        self._size += kept.size
        while self._size > self._kept_bytes:
            _key, evicted = self._kept.popitem(last=False)
            self._size -= evicted.size
