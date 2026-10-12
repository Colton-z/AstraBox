"""An Agent's tags are stored in one form, and no Agent edit is lost silently."""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent.agent_catalog import (
    MAX_AGENT_TAGS,
    normalize_tags,
)
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.providers import register_builtin_providers


def test_one_word_is_one_tag() -> None:
    assert normalize_tags(["  #Code Review ", "code-review", "PDF", "", "  "]) == [
        "code-review",
        "pdf",
    ]


def test_tags_past_the_limit_are_refused_not_cut() -> None:
    with pytest.raises(APIError) as refused:
        normalize_tags([f"tag-{n}" for n in range(MAX_AGENT_TAGS + 1)])
    assert refused.value.status_code == 400

    with pytest.raises(APIError):
        normalize_tags(["x" * 33])


class _User:
    user_id = "owner"
    roles: list[str] = []


class _RacedAgentRepo:
    """An Agent whose version another writer moved after it was read."""

    def __init__(self) -> None:
        self.agent = {
            "agent_id": "agent-1",
            "name": "researcher",
            "model": "some-model",
            "environment_name": "claude-env",
            "created_by": "owner",
            "version": 3,
            "display_meta": {"display_name": "Old"},
        }

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.agent) if agent_id == self.agent["agent_id"] else None

    async def compare_and_update_agent(
        self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        return False


class _Environments:
    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        if name == "claude-env":
            return {"name": "claude-env", "engine_kind": "claude_code"}
        return None


async def test_a_display_edit_that_loses_the_version_race_is_refused() -> None:
    register_builtin_providers()
    service = AgentConfigService(_RacedAgentRepo(), environment_repo=_Environments())  # type: ignore[arg-type]

    with pytest.raises(APIError) as refused:
        await service.upsert_agent_config(
            _User(),  # type: ignore[arg-type]
            "agent-1",
            {
                "model": "some-model",
                "environment_name": "claude-env",
                "display_meta": {"display_name": "New"},
            },
        )

    assert refused.value.code == "AGENT_VERSION_CONFLICT"
    assert refused.value.status_code == 409
