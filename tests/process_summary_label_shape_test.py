"""A process summary is one label in one JSON field, and nothing longer.

On a route that keeps reasoning, a bare plain-text request let the model
answer the user's question at length instead of naming the operations. The
request now asks for the label the way titles are asked for, and the reply is
still refused when it is not a label.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from astrabox.core.service.orchestrator.session_title_service import (
    ConversationTitleModel,
    SessionTitleGenerationError,
    TitleModelRequestConfig,
)

_CONFIG = TitleModelRequestConfig(
    base_url="https://gateway.test", api_key="test-only", model_name="label-model"
)


def _model(content: str, bodies: list[dict]) -> ConversationTitleModel:
    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return ConversationTitleModel(
        settings=SimpleNamespace(title_model_request_timeout_seconds=60),
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )


async def _summarize(content: str, bodies: list[dict]) -> str:
    return await _model(content, bodies).generate_process_summary(
        user_text="Why is the dashboard slow?",
        process_text="read nginx.conf; timed GET /api/stats; searched slow-query logs",
        request_config=_CONFIG,
    )


async def test_the_label_is_requested_as_one_json_field_after_the_data() -> None:
    bodies: list[dict] = []

    label = await _summarize(json.dumps({"label": "Read the config, timed the API"}), bodies)

    assert label == "Read the config, timed the API"
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert bodies[0]["messages"][1]["content"].endswith("Write the label for this process now as JSON:")


@pytest.mark.parametrize(
    "content",
    [
        "Read the config, timed the API",
        json.dumps({"summary": "Read the config"}),
        json.dumps({"label": "x" * 161}),
        json.dumps({"label": "Checked {cache} settings"}),
        json.dumps({"label": ""}),
    ],
    ids=["plain-text", "wrong-field", "too-long", "structured", "empty"],
)
async def test_a_reply_that_is_not_one_label_is_refused(content: str) -> None:
    with pytest.raises(SessionTitleGenerationError):
        await _summarize(content, [])
