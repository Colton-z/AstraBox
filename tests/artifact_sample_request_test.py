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
