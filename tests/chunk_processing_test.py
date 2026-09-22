"""SDK raw-event extraction helpers — the projector's shared front door.

``chunk_processing`` is the set of pure functions every stream/transcript path
leans on to pull *text*, *result-ness*, *init metadata*, and a *plain-dict form*
out of a heterogeneous Claude SDK payload. (Despite the name it is NOT a
byte-chunk reassembler — it operates on already-parsed dicts; there is no JSON /
UTF-8 boundary logic to test here.) These tests pin the precedence rules and the
snapshot short-circuit that a refactor could easily get subtly wrong.

The load-bearing subtlety pinned below: ``extract_partial_text`` first asks
``is_full_assistant_snapshot`` and returns ``""`` for a full snapshot — and a
bare ``{"content": [...]}`` payload *is* a snapshot. So the content-join branch
only ever runs for a NON-snapshot payload (one that also carries an
``event``/``chunk``/``text``/``result`` key). Getting this wrong would
double-emit a full assistant message as incremental deltas.
"""

from __future__ import annotations

from dataclasses import dataclass

from astrabox.core.service.orchestrator.chunk_processing import (
    extract_agent_session_metadata,
    extract_partial_text,
    is_result_payload,
    serialize_message,
)

# ── is_result_payload ────────────────────────────────────────────────────────


def test_is_result_payload_matches_type_or_subtype_case_insensitively() -> None:
    assert is_result_payload({"type": "result"}) is True
    assert is_result_payload({"type": "RESULT"}) is True  # lower()-folded
    assert is_result_payload({"subtype": "Result"}) is True  # subtype counts too
    assert is_result_payload({"type": "assistant"}) is False
    assert is_result_payload({}) is False


# ── extract_partial_text: precedence + snapshot short-circuit ─────────────────


def test_partial_text_precedence_direct_then_chunk() -> None:
    # Direct string `text` wins outright...
    assert extract_partial_text({"text": "abc"}) == "abc"
    # ...over a competing chunk.text (precedence, not concatenation).
    assert extract_partial_text({"text": "T", "chunk": {"text": "C"}}) == "T"
    # chunk.text is used only when there is no direct text.
    assert extract_partial_text({"chunk": {"text": "ch"}}) == "ch"


def test_partial_text_returns_empty_for_full_assistant_snapshots() -> None:
    # An explicit assistant snapshot is handled by the block path elsewhere, so
    # partial-text extraction deliberately yields "" even when a text key exists.
    assert extract_partial_text({"type": "assistant", "text": "ignored"}) == ""
    # A bare {"content": [...]} with no disambiguating key is ALSO a snapshot ->
    # "" (this is the short-circuit that stops double-emission).
    assert extract_partial_text({"content": [{"text": "snap"}]}) == ""


def test_partial_text_content_join_only_for_non_snapshot_payloads() -> None:
    # Add a disambiguating key (`chunk`) so the payload is not a snapshot; now the
    # content-join branch runs, concatenating block texts and skipping blocks
    # without a string `text`.
    raw = {"content": [{"text": "a"}, {"other": 1}, {"text": "b"}], "chunk": {}}
    assert extract_partial_text(raw) == "ab"


def test_partial_text_event_delta_beats_event_text_then_empty() -> None:
    # event.delta.text takes precedence over event.text...
    assert extract_partial_text({"event": {"delta": {"text": "D"}, "text": "T"}}) == "D"
    # ...event.text is the fallback...
    assert extract_partial_text({"event": {"text": "e"}}) == "e"
    # ...a non-string `text` is ignored (falls through), and an unrecognized
    # payload yields "".
    assert extract_partial_text({"text": 123}) == ""
    assert extract_partial_text({}) == ""


# ── extract_agent_session_metadata: init-snapshot detection ──────────────────


def test_metadata_from_explicit_init_payload() -> None:
    meta = extract_agent_session_metadata(
        {"type": "system", "subtype": "init", "model": "claude-x", "slash_commands": ["/a", "/b"]}
    )
    assert meta["model_name"] == "claude-x"
    # slash command names are cleaned of the leading "/".
    assert meta["slash_commands"] == ["a", "b"]
    assert meta["slash_command_details"] == [{"name": "a"}, {"name": "b"}]


def test_metadata_recurses_into_nested_data_envelope() -> None:
    # The init snapshot may be nested one level under `data`.
    meta = extract_agent_session_metadata({"data": {"subtype": "init", "model": "m2"}})
    assert meta == {"model_name": "m2"}


def test_metadata_detects_snapshot_by_marker_without_explicit_subtype() -> None:
    # No subtype=="init", but command metadata + an init marker ("models") make
    # this an initialization snapshot; commands become described slash-command
    # details.
    meta = extract_agent_session_metadata(
        {"commands": [{"name": "deploy", "description": "d"}], "models": ["a"], "model": "mm"}
    )
    assert meta["model_name"] == "mm"
    assert meta["slash_command_details"] == [{"name": "deploy", "description": "d"}]


def test_metadata_empty_for_non_init_and_non_dict() -> None:
    # A message that is neither subtype=init nor a snapshot yields nothing, even
    # with command metadata present (no init marker => not an init).
    assert extract_agent_session_metadata({"commands": [{"name": "x"}], "model": "mm"}) == {}
    assert extract_agent_session_metadata({"type": "assistant", "model": "m"}) == {}
    assert extract_agent_session_metadata("not-a-dict") == {}


# ── serialize_message: dict / dataclass / model_dump / attr-scrape ───────────


def test_serialize_message_shapes() -> None:
    assert serialize_message(None) == {}

    # A dict is passed straight through (same object, no copy).
    payload = {"a": 1}
    assert serialize_message(payload) is payload

    @dataclass
    class Msg:
        x: int
        y: str

    assert serialize_message(Msg(1, "z")) == {"x": 1, "y": "z"}

    class Model:
        def model_dump(self) -> dict:
            return {"m": 1}

    assert serialize_message(Model()) == {"m": 1}

    # Fallback attribute scrape: public, non-callable attributes only.
    class Plain:
        def __init__(self) -> None:
            self.pub = 5
            self._priv = 9

        def method(self) -> int:
            return 1

    assert serialize_message(Plain()) == {"pub": 5}


def test_serialize_message_skips_attributes_that_raise() -> None:
    # Intended contract: a last-resort attr scrape should be resilient — an
    # attribute whose access raises is skipped, and the sibling attributes still
    # serialize.
    class Flaky:
        ok = 3

        @property
        def bad(self) -> int:
            raise RuntimeError("attribute access blew up")

    assert serialize_message(Flaky()) == {"ok": 3}
