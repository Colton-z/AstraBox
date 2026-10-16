"""Labels for a response's folded work, written when the response ends.

A response whose history folds all of its work behind one header (see
:mod:`.history_blocks`) gets a one-line label naming that work. Two callers
ask for it:

* :meth:`ProcessSummaries.on_facts` is a fact consumer for the session readers
  (``session_runtime/platform.py`` ``reader_hooks(consumers=…)``). For every
  response a committed batch ends, whether it answered an input, the runtime
  started it, or a reader ended it for a runtime that was lost, it starts
  writing the label in the background, so the reader never waits on the model.
* :meth:`ProcessSummaries.request` answers a page that shows a header: the
  stored label, or one written now when there is none, or when the last
  attempt failed and the page asks again.

The ``process_summaries`` row is the generation claim: the writer that inserts
it calls the model, and any other caller reads the row instead of calling the
model a second time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_runtime.history_blocks import (
    Fold,
    LoadedResponse,
    load_response,
)
from astrabox.persistence.repository.process_summary_repository import ProcessSummaryRepository
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    ResponseEnd,
    SessionRuntimeRepository,
)

logger = get_logger(__name__)

SpawnTask = Callable[..., asyncio.Task[Any]]
"""``spawn(coroutine, name=...)``: run work that outlives the caller, tracked by the platform."""


class LabelModel(Protocol):
    """The model call that names a process (``session_title_service.ConversationTitleModel``)."""

    async def generate_process_summary(self, *, user_text: str, process_text: str) -> str: ...


class ProcessSummaries:
    def __init__(
        self,
        *,
        facts: SessionRuntimeRepository,
        reads: SessionReadRepository,
        repo: ProcessSummaryRepository,
        model: LabelModel,
        enabled: bool,
        spawn: SpawnTask,
    ) -> None:
        self._facts = facts
        self._reads = reads
        self._repo = repo
        self._model = model
        self._enabled = enabled
        self._spawn = spawn

    async def on_facts(self, session_id: str, batch: FactBatch) -> None:
        """Start a label for every response this batch ended."""
        if not self._enabled:
            return
        for fact in batch.facts:
            if isinstance(fact, ResponseEnd):
                self._spawn(
                    self._offer(session_id, fact.response_id),
                    name=f"process-summary-{session_id}-{fact.response_id}",
                )

    async def _offer(self, session_id: str, response_id: str) -> None:
        try:
            loaded = await load_response(self._facts, self._reads, session_id, response_id, through_seq=None)
            if loaded is None or loaded.fold is None or loaded.fold.summary_text is None:
                return
            await self._write(session_id, response_id, loaded, loaded.fold, retry_failed=False)
        except Exception:
            # Nothing awaits this task; a page that shows the header asks again.
            logger.exception("process summary offer failed session=%s response=%s", session_id, response_id)

    async def request(self, session_id: str, response_id: str, *, retry_failed: bool) -> dict[str, Any]:
        """The label for one response, written now when it has none.

        ``retry_failed`` writes again a label whose last attempt failed; a
        completed label is returned as it stands. A response whose page shows
        no single header for all of its work has nothing to name and is
        refused.
        """
        existing = (await self._repo.read(session_id, [response_id])).get(response_id)
        if existing is not None and not (retry_failed and existing["status"] == "failed"):
            return self._presented(existing)
        if not self._enabled:
            return {"status": "disabled", "summary": None, "error": None, "response_completed": None}
        loaded = await load_response(self._facts, self._reads, session_id, response_id, through_seq=None)
        if loaded is None:
            raise APIError(code="NOT_FOUND", message="response not found", status_code=404)
        if loaded.fold is None or loaded.fold.summary_text is None:
            raise APIError(
                code="INVALID_REQUEST",
                message="the response has no folded work to name",
                status_code=400,
            )
        return self._presented(
            await self._write(session_id, response_id, loaded, loaded.fold, retry_failed=retry_failed)
        )

    async def labels(self, session_id: str, response_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The stored label of each response that has one, as a header shows it."""
        rows = await self._repo.read(session_id, response_ids)
        return {
            response_id: {key: value for key, value in self._presented(row).items() if key != "response_completed"}
            for response_id, row in rows.items()
        }

    def _presented(self, row: dict[str, Any]) -> dict[str, Any]:
        presented = {
            "status": row["status"],
            "summary": row.get("summary"),
            "error": row.get("error"),
            "response_completed": row.get("response_completed"),
        }
        if not self._enabled and not presented["summary"]:
            presented.update(status="disabled", error=None)
        return presented

    async def _write(
        self,
        session_id: str,
        response_id: str,
        loaded: LoadedResponse,
        fold: Fold,
        *,
        retry_failed: bool,
    ) -> dict[str, Any]:
        """Claim, call the model and record the outcome for one response's single fold."""
        token = await self._repo.claim(
            session_id,
            response_id,
            through_seq=loaded.last_seq,
            response_completed=fold.response_completed,
            retry_failed=retry_failed,
        )
        if token is None:
            return await self._current(session_id, response_id)
        inputs = await self._reads.inputs_for_response(session_id, response_id)
        user_text = "\n\n".join(str(row.get("content") or "") for row in inputs)
        try:
            label = await self._model.generate_process_summary(
                user_text=user_text, process_text=str(fold.summary_text)
            )
        except Exception as exc:
            # The label is decoration over work the page already shows, so a
            # failed model call is recorded for a retry rather than raised.
            logger.exception("process summary failed session=%s response=%s", session_id, response_id)
            outcome: dict[str, Any] = {"status": "failed", "summary": None, "error": f"{type(exc).__name__}: {exc}"[:500]}
        else:
            outcome = {"status": "completed", "summary": label, "error": None}
        if not await self._repo.finish(session_id, response_id, claim_token=token, **outcome):
            # A retry reopened the row while the model call was out; its outcome stands.
            return await self._current(session_id, response_id)
        return {**outcome, "through_seq": loaded.last_seq, "response_completed": fold.response_completed}

    async def _current(self, session_id: str, response_id: str) -> dict[str, Any]:
        row = (await self._repo.read(session_id, [response_id])).get(response_id)
        if row is None:
            raise RuntimeError(f"process summary row of session={session_id} response={response_id} is missing")
        return row
