"""The label output cap holds reasoning on routes that cannot turn it off.

Titles, title decisions and process summaries send ``reasoning_effort:
"none"``, but an Anthropic- or OpenAI-compatible reasoning model may think
anyway, and every thinking token counts against the cap. On DeepSeek's
thinking route a 256-token cap lost 20 of 80 titles and 1024 lost 1; 2048 lost
none. The reason lives beside ``TITLE_MODEL_MAX_TOKENS`` in
``astrabox.common.utils.settings``, the one definition: the settings default,
the title service and the env registry row all read it, and
``docs/configuration.md`` is generated from the registry.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from astrabox.common.utils.settings import TITLE_MODEL_MAX_TOKENS
from astrabox.core.service.orchestrator.session_title_service import (
    ConversationTitleModel,
    TitleModelRequestConfig,
)


def test_the_label_cap_covers_measured_title_reasoning() -> None:
    assert TITLE_MODEL_MAX_TOKENS == 2048


async def test_the_cap_reaches_the_request() -> None:
    bodies: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        content = json.dumps({"title": "Label cap"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    model = ConversationTitleModel(
        settings=SimpleNamespace(title_model_request_timeout_seconds=60),
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    await model.generate_title(
        user_text="Name this",
        assistant_text="Named",
        request_config=TitleModelRequestConfig(
            base_url="https://gateway.test", api_key="test-only", model_name="label-model"
        ),
    )

    assert bodies[0]["max_tokens"] == TITLE_MODEL_MAX_TOKENS
