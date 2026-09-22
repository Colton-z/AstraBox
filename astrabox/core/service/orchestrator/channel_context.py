"""Render the channel's frozen, not-yet-submitted conversation context."""

from __future__ import annotations

from typing import Any


def channel_turn_content(content: str, context: list[dict[str, Any]]) -> str:
    if not context:
        return content
    rows = ["以下是 Bot 未参与期间、当前会话中的群聊记录（按时间顺序）："]
    for message in context:
        recall = message.get("recall")
        text = (
            f"[该消息已于 {recall['timestamp']} 被撤回]"
            if isinstance(recall, dict)
            else str(message["content"])
        )
        rows.append(
            f"- [{message['timestamp']}] {message['participant']}：{text}"
        )
    rows.extend(("", "当前唤起 Bot 的消息：", content))
    return "\n".join(rows)
