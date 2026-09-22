from __future__ import annotations

import pytest

from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    InputConsumed,
    InteractionRequested,
    PrivateDiagnostic,
    PublicUIFrame,
    TurnTerminal,
    emission_from_translated_frame,
    public_ui_frame,
)


@pytest.mark.parametrize("frame_type", ["tool-input-start", "tool-input-available", "tool-input-error"])
@pytest.mark.parametrize("dynamic", [None, False, True])
def test_tool_presentation_is_owned_by_the_public_boundary(frame_type: str, dynamic: bool | None) -> None:
    frame = {
        "type": frame_type,
        "toolCallId": "native-call",
        "toolName": "supplier/arbitrary-tool",
        "input": {"vendor-node": [{"anything": True}]},
        "providerMetadata": {"vendor": {"native": 42}},
        "toolMetadata": {"nested": ["unchanged"]},
        "errorText": "supplier error, unchanged",
        "vendor-added": {"future": True},
        **({"dynamic": dynamic} if dynamic is not None else {}),
    }
    expected = {**frame, "dynamic": True}
    assert public_ui_frame(frame) == expected
    assert PublicUIFrame(frame).as_frame() == expected
    emission = emission_from_translated_frame(frame)
    assert isinstance(emission, PublicUIFrame)
    assert emission.as_frame() == expected
    assert frame.get("dynamic") is dynamic
    output = emission.as_frame()
    output["input"]["vendor-node"].clear()
    assert emission["input"] == frame["input"]


def test_public_extension_payload_is_open_after_adapter_translation() -> None:
    frame = {
        "type": "data-vendor-extension",
        "providerMetadata": {"cacheReadTokens": 12},
        "data": {"newField": {"nested": True}},
    }

    emission = emission_from_translated_frame(frame)

    assert isinstance(emission, PublicUIFrame)
    assert emission.as_frame() == frame


def test_platform_envelope_classification_is_shared_by_every_adapter() -> None:
    public = emission_from_translated_frame(
        {"type": "data-new-engine-extension", "data": {"added": True}}
    )
    private = emission_from_translated_frame(
        {
            "type": "data-raw-event",
            "data": {"subtype": "vendor.telemetry", "raw": {"native": "secret"}},
        }
    )
    child = emission_from_translated_frame(
        {
            "type": "data-child-resource",
            "__engine_frame_scope": "session",
            "data": {"controlRef": "private-control"},
        }
    )

    assert isinstance(public, PublicUIFrame)
    assert isinstance(private, PrivateDiagnostic)
    assert isinstance(child, ChildResourceFact)


def test_raw_vendor_event_is_structurally_private() -> None:
    emission = emission_from_translated_frame(
        {
            "type": "data-raw-event",
            "data": {
                "event_type": "vendor.protocol",
                "subtype": "telemetry.added",
                "raw": {"nativeSessionId": "private-session"},
            },
        }
    )

    assert isinstance(emission, PrivateDiagnostic)
    assert not isinstance(emission, PublicUIFrame)
    assert emission.raw == {"nativeSessionId": "private-session"}


def test_session_owned_raw_fact_stays_on_the_private_child_projection() -> None:
    emission = emission_from_translated_frame(
        {
            "type": "data-raw-event",
            "__engine_frame_scope": "session",
            "data": {"controlRef": "private-control"},
        }
    )

    assert isinstance(emission, ChildResourceFact)


def test_platform_control_categories_are_closed_types() -> None:
    consumed = emission_from_translated_frame(
        {
            "type": "data-input-consumed",
            "data": {
                "inputId": "input-1",
                "responseMessageId": "response-1",
                "content": "hello",
            },
        }
    )
    interaction = emission_from_translated_frame(
        {
            "type": "interaction.request",
            "interactionId": "interaction-1",
            "payload": {
                "presentation": "tool_approval",
                "tool_name": "Bash",
                "prompt": "Continue?",
                "raw_input": {"command": "pwd"},
            },
        }
    )
    terminal = emission_from_translated_frame(
        {
            "type": "result",
            "finishReason": "error",
            "__engine_terminal_reason": "refusal",
            "error": {"code": "ENGINE_FAILED", "message": "failed"},
            "vendorReason": {"kind": "refusal"},
        }
    )

    assert isinstance(consumed, InputConsumed)
    assert consumed.input_id == "input-1"
    assert isinstance(interaction, InteractionRequested)
    assert interaction.interaction_id == "interaction-1"
    assert isinstance(terminal, TurnTerminal)
    assert terminal.outcome == "failed"
    assert terminal.native_reason == "refusal"
    assert terminal.private_data == {"vendorReason": {"kind": "refusal"}}
    assert terminal["vendorReason"] == {"kind": "refusal"}


def test_unknown_platform_terminal_outcome_fails_at_the_adapter_boundary() -> None:
    with pytest.raises(ValueError, match="finishReason"):
        emission_from_translated_frame(
            {"type": "result", "finishReason": "vendor-added-state"}
        )


def test_private_or_session_frames_cannot_become_public() -> None:
    raw = emission_from_translated_frame({"type": "data-raw-event", "data": {}})
    child = emission_from_translated_frame(
        {
            "type": "data-child-run",
            "__engine_frame_scope": "session",
        }
    )

    assert isinstance(raw, PrivateDiagnostic)
    assert isinstance(child, ChildResourceFact)
