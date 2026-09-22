from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.core.service.orchestrator.engine.base import EngineAdapter
from astrabox.core.service.orchestrator.engine.child_runs import public_child_run_id
from astrabox.core.service.orchestrator.engine.emissions import ChildResourceFact
from astrabox.core.service.orchestrator.engine.frame_scope import session_scoped_engine_frame
from astrabox.core.service.orchestrator import session_child_run_view as child_view


class _StoredTranscriptAdapter(EngineAdapter):
    """An engine that answers child history from its stored native transcript."""

    def __init__(self, reader: Mock) -> None:
        self._reader = reader

    @property
    def engine_kind(self) -> str:  # type: ignore[override]
        return "stored"

    @property
    def engine_client_type(self) -> type:  # type: ignore[override]
        raise AssertionError("not part of the child history read")

    @property
    def capabilities(self):  # type: ignore[override]
        raise AssertionError("not part of the child history read")

    def sandbox_request(self, *, template, model_access):
        raise AssertionError("child history must not need a sandbox")

    async def activate_runtime(self, context):
        raise AssertionError("child history must not need a runtime")

    def stored_child_transcript_facts(
        self,
        *,
        engine_ref: str,
        closed: bool,
        raw_scopes: list[dict],
        raw_messages: list[dict],
    ) -> list[ChildResourceFact]:
        return self._reader(
            engine_ref=engine_ref,
            closed=closed,
            raw_scopes=raw_scopes,
            raw_messages=raw_messages,
        )


def _fact(kind: str, **fields: object) -> ChildResourceFact:
    return ChildResourceFact(session_scoped_engine_frame({
        "type": "data-subagent",
        "id": f"stored-child:{kind}",
        "data": {"kind": kind, "engineKind": "stored", "engineRef": "child", **fields},
    }))


def _view(monkeypatch: pytest.MonkeyPatch, *, read_error: bool = False):
    final = "child output " * 300 + "\n```acceptance-report\n{\"complete\":true}\n```"
    native = [{"vendor": "opaque", "content": final}]
    lifecycle = _fact(
        "lifecycle", event="closed", engineEvent="finished", engineStatus="complete", operations=[]
    )
    preview = _fact(
        "message", role="assistant", content=[{"type": "text", "text": final[:1000]}],
        messageId="inspection-preview",
    )
    events = SimpleNamespace(
        list_events=AsyncMock(return_value=[]),
        list_frames=AsyncMock(return_value=[
            {"frame_seq": index, "scope": "session", "payload": fact.as_frame()}
            for index, fact in enumerate([lifecycle, preview])
        ]),
    )
    scope = {"project_key": "project", "session_id": "native", "subpath": "opaque-scope"}
    repository = SimpleNamespace(
        list_scopes_by_platform_session=AsyncMock(return_value=[scope]),
        load_subpath_entries_by_platform_session=AsyncMock(return_value=native),
    )
    if read_error:
        repository.load_subpath_entries_by_platform_session.side_effect = RuntimeError("database read failed")
    reader = Mock(return_value=[_fact(
        "message", role="assistant", content=[{"type": "text", "text": final}], messageId="opaque-scope:1"
    )])
    adapter = _StoredTranscriptAdapter(reader)
    monkeypatch.setattr(child_view, "get_engine_adapter", lambda _: adapter)
    view = child_view.SessionChildRunView(events, transcript_entries_repo=repository)
    child_id = public_child_run_id(session_id="session", engine_kind="stored", engine_ref="child")
    return view, child_id, reader, repository, scope, native, final


@pytest.mark.asyncio
async def test_stored_child_history_replaces_previews_and_is_stable_without_a_sandbox(monkeypatch):
    view, child_id, reader, repository, scope, native, final = _view(monkeypatch)
    first = await view.get_child_run_messages("session", child_id)
    assert first is not None
    assert len(first) == 1
    assert first[0]["content"] == [{"type": "text", "text": final}]
    assert await view.get_child_run_messages("session", child_id) == first
    reader.assert_called_with(engine_ref="child", closed=True, raw_scopes=[{**scope, "entries": native}], raw_messages=[])
    repository.load_subpath_entries_by_platform_session.assert_called_with("session", subpath="opaque-scope")


@pytest.mark.asyncio
async def test_stored_child_history_read_failure_does_not_return_a_preview(monkeypatch):
    view, child_id, reader, _, _, _, _ = _view(monkeypatch, read_error=True)
    with pytest.raises(RuntimeError, match="database read failed"):
        await view.get_child_run_messages("session", child_id)
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_stored_child_history_cannot_create_an_unknown_child(monkeypatch):
    view, _, reader, repository, _, _, _ = _view(monkeypatch)
    assert await view.get_child_run_messages("session", "unknown") is None
    reader.assert_not_called()
    repository.list_scopes_by_platform_session.assert_not_called()
