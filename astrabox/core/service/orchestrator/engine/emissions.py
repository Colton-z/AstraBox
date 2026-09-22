"""Typed output contract between an engine adapter and orchestration.

Adapters translate their native protocol before crossing this boundary. A
browser-facing frame is deliberately open: once an adapter emits a translated
UI frame, additive AI SDK fields and ``data-*`` payloads pass through without a
platform registry. Control facts and raw diagnostics use reserved platform
envelopes, so they cannot become public merely because a vendor adds a field.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias


TurnOutcome = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class SessionMessageFact:
    """An engine-authored text message that does not belong to a user turn."""

    message_id: str
    content: str

    def __post_init__(self) -> None:
        if not self.message_id.strip() or not self.content.strip():
            raise ValueError("session message requires an identity and nonempty text")

_PLATFORM_ENVELOPE_CATEGORIES = {
    "data-raw-event": "private_diagnostic",
    "data-input-consumed": "input_consumed",
    "interaction.request": "interaction_requested",
    "background-tasks-opened": "background_tasks_opened",
    "response-result": "response_completed",
    "result": "turn_terminal",
}


class EngineEmission(Mapping[str, Any]):
    """One adapter-authored output with a mapping view for adapter tests.

    The mapping is the adapter's translated frame, not the platform contract.
    Orchestration dispatches on the concrete emission type and only calls
    :meth:`as_frame` for emissions whose type permits durable UI projection.
    """

    frame: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.frame[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.frame)

    def __len__(self) -> int:
        return len(self.frame)

    def as_frame(self) -> dict[str, Any]:
        """Return an isolated copy of the adapter's translated frame."""

        return deepcopy(self.frame)

    @property
    def engine_sequence_number(self) -> int | None:
        value = self.frame.get("__engine_sequence_number")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None


def _copied_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    copied = deepcopy(dict(frame))
    frame_type = str(copied.get("type") or "").strip()
    if not frame_type:
        raise ValueError("engine emission requires a non-empty frame type")
    copied["type"] = frame_type
    return copied


def public_ui_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap an open UI payload in the console's AI SDK presentation contract.

    Tools are discovered at runtime, not registered in a browser tool schema.
    AI SDK 7 selects ``dynamic-tool`` on these input chunks. Tool identity,
    arguments, metadata and outcome remain the adapter's unchanged payload.
    """

    copied = _copied_frame(frame)
    if copied["type"] in {
        "tool-input-start", "tool-input-available", "tool-input-error",
    }:
        copied["dynamic"] = True
    return copied


@dataclass(frozen=True, slots=True, eq=False)
class PublicUIFrame(EngineEmission):
    """An adapter-declared browser frame with an open payload."""

    frame: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", public_ui_frame(self.frame))


@dataclass(frozen=True, slots=True, eq=False)
class InputConsumed(EngineEmission):
    """Proof that an engine dequeued one exact platform FIFO input."""

    frame: dict[str, Any]
    input_id: str
    response_message_id: str
    content: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _copied_frame(self.frame))


@dataclass(frozen=True, slots=True, eq=False)
class InteractionRequested(EngineEmission):
    """An adapter-owned interaction expressed in the platform presentation."""

    frame: dict[str, Any]
    interaction_id: str
    contract: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _copied_frame(self.frame))
        object.__setattr__(self, "contract", deepcopy(self.contract))


@dataclass(frozen=True, slots=True, eq=False)
class ChildResourceFact(EngineEmission):
    """A private engine-owned child resource fact for the Session read model."""

    frame: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _copied_frame(self.frame))


@dataclass(frozen=True, slots=True, eq=False)
class BackgroundTasksOpened(EngineEmission):
    """The durable transcript/control manifest for detached child work."""

    frame: dict[str, Any]
    manifest: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _copied_frame(self.frame))
        object.__setattr__(self, "manifest", deepcopy(self.manifest))


@dataclass(frozen=True, slots=True, eq=False)
class ResponseCompleted(EngineEmission):
    """One native FIFO response ended while the attached stream continues."""

    frame: dict[str, Any]
    public_data: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _copied_frame(self.frame))
        object.__setattr__(self, "public_data", deepcopy(self.public_data))


@dataclass(frozen=True, slots=True, eq=False)
class TurnTerminal(EngineEmission):
    """The adapter's explicit terminal judgement for the active turn."""

    frame: dict[str, Any]
    outcome: TurnOutcome
    finish_reason: str
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    native_reason: str | None = None
    private_data: dict[str, Any] | None = None
    closes_interaction: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _copied_frame(self.frame))
        object.__setattr__(self, "usage", deepcopy(self.usage))
        object.__setattr__(self, "error", deepcopy(self.error))
        object.__setattr__(self, "private_data", deepcopy(self.private_data))


@dataclass(frozen=True, slots=True, eq=False)
class PrivateDiagnostic(EngineEmission):
    """Raw adapter evidence stored for operators and never sent to a browser."""

    frame: dict[str, Any]
    event_type: str
    subtype: str
    raw: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", _copied_frame(self.frame))
        object.__setattr__(self, "raw", deepcopy(self.raw))


EngineTurnEmission: TypeAlias = (
    PublicUIFrame
    | InputConsumed
    | InteractionRequested
    | ChildResourceFact
    | BackgroundTasksOpened
    | ResponseCompleted
    | TurnTerminal
    | PrivateDiagnostic
)


def emission_from_translated_frame(
    frame: Mapping[str, Any],
) -> EngineTurnEmission:
    """Classify and validate one translated platform envelope.

    Adapters own native-to-platform translation; the envelope names below are
    platform vocabulary and therefore have one classifier here. Unknown frame
    names are public because reaching this function proves the adapter chose to
    translate them as UI output. Their payload remains open so additive AI SDK
    and vendor presentation fields pass through without a core allowlist.
    """

    copied = _copied_frame(frame)
    frame_type = copied["type"]
    if str(copied.get("__engine_frame_scope") or "").strip() == "session":
        category = "child_resource"
    else:
        category = _PLATFORM_ENVELOPE_CATEGORIES.get(frame_type, "public_ui")

    if category == "child_resource":
        if str(copied.get("__engine_frame_scope") or "").strip() != "session":
            raise ValueError("child_resource requires Session-scoped frame ownership")
        return ChildResourceFact(copied)

    if category == "private_diagnostic":
        if frame_type != "data-raw-event":
            raise ValueError("private_diagnostic requires data-raw-event")
        data = copied.get("data")
        data = data if isinstance(data, dict) else {}
        return PrivateDiagnostic(
            copied,
            event_type=str(data.get("event_type") or "engine"),
            subtype=str(data.get("subtype") or "unknown"),
            raw=data.get("raw"),
        )

    if category == "input_consumed":
        if frame_type != "data-input-consumed":
            raise ValueError("input_consumed requires data-input-consumed")
        data = copied.get("data")
        if not isinstance(data, dict):
            raise ValueError("data-input-consumed requires a data object")
        input_id = str(data.get("inputId") or "").strip()
        response_message_id = str(data.get("responseMessageId") or "").strip()
        content = data.get("content")
        if not input_id or not response_message_id or not isinstance(content, str):
            raise ValueError(
                "data-input-consumed requires inputId, responseMessageId, and content"
            )
        return InputConsumed(copied, input_id, response_message_id, content)

    if category == "interaction_requested":
        if frame_type != "interaction.request":
            raise ValueError("interaction_requested requires interaction.request")
        interaction_id = str(copied.get("interactionId") or "").strip()
        contract = copied.get("payload")
        if not isinstance(contract, dict):
            raise ValueError("interaction.request requires a payload object")
        return InteractionRequested(copied, interaction_id, contract)

    if category == "background_tasks_opened":
        if frame_type != "background-tasks-opened":
            raise ValueError(
                "background_tasks_opened requires background-tasks-opened"
            )
        manifest = copied.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("background-tasks-opened requires a manifest object")
        return BackgroundTasksOpened(copied, manifest)

    if category == "response_completed":
        if frame_type != "response-result":
            raise ValueError("response_completed requires response-result")
        data = copied.get("data")
        if not isinstance(data, dict):
            raise ValueError("response-result requires a data object")
        return ResponseCompleted(copied, data)

    if category == "turn_terminal":
        if frame_type != "result":
            raise ValueError("turn_terminal requires result")
        finish_reason = str(copied.get("finishReason") or "").strip().lower()
        outcomes: dict[str, TurnOutcome] = {
            "stop": "completed",
            "error": "failed",
            "cancelled": "cancelled",
        }
        outcome = outcomes.get(finish_reason)
        if outcome is None:
            raise ValueError(
                "result requires finishReason in {'stop', 'error', 'cancelled'}"
            )
        usage = copied.get("usage")
        error = copied.get("error")
        private_data = {
            key: deepcopy(value)
            for key, value in copied.items()
            if key not in {"type", "finishReason", "usage", "error"}
            and not str(key).startswith("__")
        }
        return TurnTerminal(
            copied,
            outcome=outcome,
            finish_reason=finish_reason,
            usage=dict(usage) if isinstance(usage, dict) else None,
            error=dict(error) if isinstance(error, dict) else None,
            native_reason=(
                str(copied.get("__engine_terminal_reason") or "").strip() or None
            ),
            private_data=private_data or None,
            closes_interaction=copied.get("__interaction_closed") is True,
        )

    if category == "public_ui":
        return PublicUIFrame(copied)

    raise AssertionError(f"unhandled engine emission category: {category!r}")
