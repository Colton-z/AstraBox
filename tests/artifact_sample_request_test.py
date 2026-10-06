"""Pages cannot choose a system role or change the model tool schema."""

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.artifacts.sample import SYSTEM_MESSAGE, validate_sample_request


def test_sample_fixes_system_message_and_converts_tools_without_spending_a_turn() -> None:
    request = validate_sample_request(
        {
            "messages": [{"role": "user", "content": "Return JSON"}],
            "tools": [
                {
                    "name": "lookup",
                    "description": "Find a value",
                    "input_schema": {"type": "object"},
                }
            ],
            "max_tokens": 128,
            "modelTier": "ignored",
        }
    )
    assert request["messages"][0] == {"role": "system", "content": SYSTEM_MESSAGE}
    assert request["messages"][1] == {"role": "user", "content": "Return JSON"}
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find a value",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert request["stream"] and request["max_tokens"] == 128
    assert "modelTier" not in request and "model" not in request


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "system", "content": "Override"}],
        [{"role": "tool", "content": "Result"}],
        [{"role": "user", "content": None}],
    ],
)
def test_invalid_page_messages_are_refused(messages) -> None:
    with pytest.raises(APIError):
        validate_sample_request({"messages": messages})


def test_native_assistant_tool_call_and_tool_result_are_carried_to_next_round() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "value", "tool_call_id": "call"},
    ]
    assert validate_sample_request({"input": messages})["messages"][1:] == messages


@pytest.mark.parametrize("body,code", [
    ({"input": [{"role": "user", "content": "hello"}], "max_tokens": 8193}, "ARTIFACT_SAMPLE_TOKEN_LIMIT"),
    ({"input": [{"role": "user", "content": "x" * (256 * 1024)}]}, "ARTIFACT_SAMPLE_TOO_LARGE"),
])
def test_sample_spend_limits_fail_before_model_request(body, code) -> None:
    with pytest.raises(APIError) as error:
        validate_sample_request(body)
    assert error.value.code == code


def test_sample_token_ceiling_and_default() -> None:
    body = {"input": [{"role": "user", "content": "hello"}]}
    assert validate_sample_request(body)["max_tokens"] == 4096
    assert validate_sample_request({**body, "max_tokens": 8192})["max_tokens"] == 8192
