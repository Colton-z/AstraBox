"""The Result's store coordinate is a lower bound, and a Result without one fails.

Three properties, and the middle one is the load-bearing one. The coordinate
says how far the transcript is KNOWN to be committed; it is allowed to lag, and
it must never lead. A consumer trims the journal up to it, so a value that is
too low wastes memory and a value that is too high discards transcript that was
never stored.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.claude_code_client import (
    require_result_store_sequence,
)
from astrabox.core.service.orchestrator.engine.runner_link import RunnerLinkError
from astrabox.core.service.orchestrator.sandbox_runner import (
    SpoolSessionStore,
    _HttpStoreTarget,
)

MAIN_KEY = {"project_key": "p", "session_id": "s", "subpath": ""}
SUB_KEY = {"project_key": "p", "session_id": "s", "subpath": "sidechain"}


async def _empty_subkeys(_key: dict[str, Any]) -> list[str]:
    return []


def _confirm(target: _HttpStoreTarget, key: dict[str, Any], sequence: int) -> None:
    """Replay what a successful flush response does to the target."""
    target._record_store_sequence(key, {"store_sequence": sequence})


def test_the_coordinate_is_zero_before_anything_is_confirmed() -> None:
    target = _HttpStoreTarget("http://platform", {})
    assert target.confirmed_store_sequence() == 0


def test_the_coordinate_advances_with_confirmed_commits() -> None:
    target = _HttpStoreTarget("http://platform", {})
    _confirm(target, MAIN_KEY, 4)
    assert target.confirmed_store_sequence() == 4
    _confirm(target, MAIN_KEY, 9)
    assert target.confirmed_store_sequence() == 9


def test_an_unflushed_batch_does_not_advance_the_coordinate(tmp_path: Any) -> None:
    """The property the whole contract rests on: it lags, it never leads.

    ``append`` acks once the batch is on disk, so a spooled batch the platform
    has not answered for must not move the number. A test that only checked
    "the field is present" would pass an implementation that counted local
    appends, which is exactly the optimistic value that loses transcript.
    """
    target = _HttpStoreTarget("http://platform", {})
    flushed: list[str] = []

    async def flush(key: dict[str, Any], entries: list[dict[str, Any]], append_id: str) -> None:
        flushed.append(append_id)

    async def load(key: dict[str, Any]) -> list[dict[str, Any]] | None:
        return None

    store = SpoolSessionStore(
        tmp_path,
        flush_fn=flush,
        load_fn=load,
        list_subkeys_fn=_empty_subkeys,
        sequence_fn=target.confirmed_store_sequence,
    )
    _confirm(target, MAIN_KEY, 7)

    import asyncio

    asyncio.run(store.append(MAIN_KEY, [{"type": "user"}]))
    assert store.pending_batch_count() == 1, "the batch must be spooled and unflushed"
    assert store.confirmed_store_sequence() == 7, (
        "a spooled batch the platform has not confirmed advanced the coordinate; "
        "the value now claims durability the store does not have"
    )


def test_a_subpath_scope_does_not_answer_for_the_main_one() -> None:
    """Scopes carry independent sequences, so they are never mixed."""
    target = _HttpStoreTarget("http://platform", {})
    _confirm(target, SUB_KEY, 99)
    assert target.confirmed_store_sequence() == 0, (
        "a sidechain scope's position was reported as the main scope's; the two "
        "count different things and comparing them is meaningless"
    )
    _confirm(target, MAIN_KEY, 3)
    assert target.confirmed_store_sequence() == 3


def test_a_store_with_no_reader_answers_zero(tmp_path: Any) -> None:
    async def flush(key: dict[str, Any], entries: list[dict[str, Any]], append_id: str) -> None:
        return None

    async def load(key: dict[str, Any]) -> list[dict[str, Any]] | None:
        return None

    store = SpoolSessionStore(
        tmp_path,
        flush_fn=flush,
        load_fn=load,
        list_subkeys_fn=_empty_subkeys,
    )
    assert store.confirmed_store_sequence() == 0


def test_the_key_identity_round_trips_for_the_scope_check() -> None:
    """The reader parses the identity it wrote; pin the shape they share."""
    identity = _HttpStoreTarget._key_identity(MAIN_KEY)
    assert json.loads(identity) == ["p", "s", ""]


@pytest.mark.parametrize("value", [None, "3", True, 1.5, -1])
def test_a_result_without_an_integer_coordinate_is_refused(value: Any) -> None:
    """The host treats a Result with no usable coordinate as a wire violation.

    Drives the production check rather than restating it: a test that
    re-implemented the condition would pass with no check in the client at all.
    """
    frame: dict[str, Any] = {"message_type": "ResultMessage"}
    if value is not None:
        frame["store_sequence"] = value
    with pytest.raises(RunnerLinkError):
        require_result_store_sequence(frame)


def test_a_result_with_a_coordinate_returns_it() -> None:
    assert require_result_store_sequence({"store_sequence": 0}) == 0
    assert require_result_store_sequence({"store_sequence": 12}) == 12
