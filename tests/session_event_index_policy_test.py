"""Session-event index policy: which index failures block, which degrade.

``_ensure_indexes_once`` guards two *unique* indexes with very different roles:

* ``ux_session_events_seq`` (session_id+event_seq) is load-bearing —
  ``append_event``'s collision-retry seq allocation is only exactly-once if
  the index is real. Its verification failure must propagate (fail loud).
* ``ux_session_events_idempotency_key`` (sparse) is defense-in-depth
  metadata — ``try_claim_event`` fences exactly-once on the deterministic
  ``_id``, not on this index (see its docstring). Its failure must degrade to
  a warning, NOT take down every event read/append behind the shared
  ``ensure_indexes()`` gate (the managed-Mongo case: DBAs pre-created the
  load-bearing index but missed the sparse secondary one).

These are unit tests over the policy split itself; the real-mongod wiring
proof lives in ``tests/mongo_index_verification_test.py`` (``-m mongo``).
"""

from __future__ import annotations

import pytest

import astrabox.persistence.repository.session_event_repository as events_module


class _FakeCollection:
    """Just enough surface for ``_ensure_indexes_once``'s non-unique calls."""

    async def create_index(self, *args, **kwargs):
        return kwargs.get("name")


def _selective_ensure(failing_name: str, attempted: list[str]):
    async def _fake_ensure_unique_index(collection, keys, *, name=None, **kwargs):
        attempted.append(name)
        if name == failing_name:
            raise RuntimeError(f"required unique index is missing: {name}")

    return _fake_ensure_unique_index


async def test_idempotency_index_failure_degrades_to_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[str] = []
    monkeypatch.setattr(
        events_module,
        "ensure_unique_index",
        _selective_ensure(events_module._IDEMPOTENCY_INDEX_NAME, attempted),
    )

    # Must complete: the load-bearing seq index verified fine, so read/append
    # paths stay up even though the sparse secondary index is unavailable.
    await events_module._ensure_indexes_once(_FakeCollection())

    assert attempted == [
        "ux_session_events_seq",
        events_module._IDEMPOTENCY_INDEX_NAME,
    ], f"both unique indexes must still be attempted, got {attempted!r}"


async def test_seq_index_failure_still_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[str] = []
    monkeypatch.setattr(
        events_module,
        "ensure_unique_index",
        _selective_ensure("ux_session_events_seq", attempted),
    )

    with pytest.raises(RuntimeError, match="ux_session_events_seq"):
        await events_module._ensure_indexes_once(_FakeCollection())

    assert attempted == ["ux_session_events_seq"], (
        "the load-bearing index is verified first and its failure must "
        f"propagate before any secondary work, got {attempted!r}"
    )
