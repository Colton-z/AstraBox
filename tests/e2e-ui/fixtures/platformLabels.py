"""Observe real title/summary responses through the deployment's model route."""

from __future__ import annotations

import asyncio
import json

import httpx

from astrabox.deploy.onebox import ensure_database_wiring

ensure_database_wiring()

from astrabox.providers import register_builtin_providers  # noqa: E402
from astrabox.core.service.orchestrator.session_title_service import ConversationTitleModel  # noqa: E402

register_builtin_providers()
observations: list[dict[str, object]] = []


class ObservedClient(httpx.AsyncClient):
    async def post(self, *args, **kwargs):
        response = await super().post(*args, **kwargs)
        response.raise_for_status()
        payload = response.json()
        choice = payload["choices"][0]
        message = choice["message"]
        usage = payload.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        observation = {
            "model": kwargs["json"]["model"],
            "reasoning_chars": len(message.get("reasoning_content") or ""),
            "reasoning_tokens": details.get("reasoning_tokens") or 0,
            "finish_reason": choice["finish_reason"],
            "content_present": bool(message.get("content")),
        }
        observations.append(observation)
        print(json.dumps(observation), flush=True)
        assert observation["reasoning_chars"] == 0, "label response generated reasoning"
        assert observation["reasoning_tokens"] == 0, "label response spent reasoning tokens"
        assert observation["finish_reason"] == "stop", "label output exhausted its budget"
        assert observation["content_present"], "label response has no final text"
        return response


async def main() -> None:
    model = ConversationTitleModel(http_client_factory=ObservedClient)
    user_text = "检索小米最新研报，核对日期并整理业务进展。"
    process = "\n".join(
        f"检索第{i}条研报，读取公司与日期，核对来源并保存摘要。"
        for i in range(180)
    )
    title = await model.generate_title(user_text=user_text, assistant_text=process)
    summary = await model.generate_process_summary(user_text=user_text, process_text=process)
    assert title.strip() and summary.strip()
    assert len(observations) == 2
    print("PLATFORM_LABELS_NO_REASONING_PASS", flush=True)


asyncio.run(main())
