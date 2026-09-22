"""Platform labels disable reasoning independently of the Agent's settings."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from astrabox.core.service.orchestrator.session_title_service import (
    ConversationTitleModel,
    SessionTitleGenerationError,
    TitleModelRequestConfig,
    _extract_chat_completion_content,
)


def test_a_reasoning_only_response_names_the_disabled_reasoning_contract() -> None:
    payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"role": "assistant", "content": None, "reasoning_content": "thinking..."},
            }
        ]
    }
    with pytest.raises(SessionTitleGenerationError) as excinfo:
        _extract_chat_completion_content(payload)
    assert "reasoning_effort=none" in str(excinfo.value)
    assert "ASTRABOX_TITLE_MODEL_MAX_TOKENS" not in str(excinfo.value)


def test_an_empty_response_without_reasoning_does_not_blame_the_budget() -> None:
    payload = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": ""}}]}
    with pytest.raises(SessionTitleGenerationError) as excinfo:
        _extract_chat_completion_content(payload)
    assert "ASTRABOX_TITLE_MODEL_MAX_TOKENS" not in str(excinfo.value)


@pytest.mark.parametrize("operation", ["title", "decision", "process"])
async def test_platform_label_requests_explicitly_disable_reasoning(operation: str) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["reasoning_effort"] == "none"
        assert "chat_template_kwargs" not in body
        content = (
            "检索并核对行业文章"
            if operation == "process"
            else json.dumps({"title": "行业文章检索", "should_generate": True})
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    model = ConversationTitleModel(
        settings=SimpleNamespace(title_model_request_timeout_seconds=60),
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    config = TitleModelRequestConfig(
        base_url="https://gateway.test/v1", api_key="test-only", model_name="label-model"
    )
    if operation == "process":
        result = await model.generate_process_summary(
            user_text="检索行业文章", process_text="检索文章并核对日期", request_config=config
        )
        assert result == "检索并核对行业文章"
    elif operation == "decision":
        decision = await model.decide_title_generation(
            user_text="检索行业文章",
            assistant_text="找到三篇文章",
            turn_index=1,
            request_config=config,
        )
        assert decision.should_generate
        assert decision.title == "行业文章检索"
    else:
        result = await model.generate_title(
            user_text="检索行业文章", assistant_text="找到三篇文章", request_config=config
        )
        assert result == "行业文章检索"
    assert len(requests) == 1
