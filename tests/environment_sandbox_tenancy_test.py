"""How many conversations a box carries is the environment's to say.

Isolated sessions are the SANDBOX's capability, so the knob that turns them on
is a statement about boxes, not about the Agent program running inside one. Keeping
the value on the Environment also prevents a program-specific field from
claiming a sandbox capability that the selected environment cannot provide.

The chain under test is an Environment document, through the Agent runtime
resolver, into a tenancy, into the adapter's rendering of one conversation at
that tenancy, into the ``ConversationIdentity`` the backend receives. The
end-to-end assertion fails if any link stops carrying the value.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

import astrabox.providers as providers
import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 — registers claude_code
import astrabox.core.service.orchestrator.engine.hermes  # noqa: F401 — registers assistant
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    conversation_identity_from_plan,
)
from astrabox.core.service.orchestrator.environment_schema import get_environment_schema
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    resolve_sandbox_permission_level,
    resolve_runtime_profile,
)

_AGENT_ID = "agent-1"

providers.register_builtin_providers()


class _FakeEnvRepo:
    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        return self._doc if name == self._doc.get("name") else None


class _FakeAgentRepo:
    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        if agent_id != _AGENT_ID:
            return None
        return {"agent_id": _AGENT_ID, "name": "a", "environment_name": "env-1"}


async def _harness(**environment: Any) -> Any:
    service = AgentConfigService(
        agent_repo=_FakeAgentRepo(),  # type: ignore[arg-type]
        environment_repo=_FakeEnvRepo({"name": "env-1", **environment}),  # type: ignore[arg-type]
        assistant_repo=object(),  # type: ignore[arg-type]  # unused on this path
    )
    return await service.resolve_agent_harness(_AGENT_ID)


class _Plan:
    agent_id = _AGENT_ID


def _identity(profile: Any, home: str) -> Any:
    return conversation_identity_from_plan(
        {
            "sandbox_tenancy": profile.sandbox_tenancy,
            "home_dir": home,
            "workspace_dir": "/workspace",
            "workspace_source_dir": (
                f"{home}/workspace"
                if profile.sandbox_tenancy == "agent"
                else "/workspace"
            ),
        },
        _Plan(),
    )


async def test_agent_tenancy_reaches_the_backend_as_aconversation_identity_from_plan() -> None:
    view = await _harness(engine_kind="claude_code", sandbox_tenancy="agent")
    assert view is not None
    # If the field does not survive the environment overlay, the profile below
    # silently becomes the per-conversation one and nothing else notices.
    assert view.sandbox_tenancy == "agent"

    profile = resolve_runtime_profile(view, session_kind="agent_chat")
    assert profile.sandbox_tenancy == "agent"
    # The per-conversation home is what makes sharing possible at all: a fixed
    # one would have two conversations chown the same directory.
    assert profile.home_template == "/home/conversations/{username}"

    identity = _identity(profile, "/home/conversations/conv_abc")
    assert identity is not None
    assert identity.agent_id == _AGENT_ID
    assert identity.home_dir == "/home/conversations/conv_abc"
    assert identity.workspace_dir == "/workspace"
    assert identity.workspace_source_dir == "/home/conversations/conv_abc/workspace"
    assert "useradd" in profile.required_commands
    assert "groupadd" in profile.required_commands


async def test_sandbox_permission_level_survives_the_environment_overlay() -> None:
    """A declared grant must reach provisioning instead of becoming default."""
    view = await _harness(
        engine_kind="codex",
        sandbox_permission_level="advanced",
    )

    assert view.sandbox_permission_level == "advanced"
    assert resolve_sandbox_permission_level(view) == "advanced"


@pytest.mark.parametrize("environment", [{}, {"sandbox_tenancy": "conversation"}])
async def test_the_default_and_the_explicit_default_are_one_box_each(
    environment: dict[str, Any],
) -> None:
    view = await _harness(engine_kind="claude_code", **environment)
    profile = resolve_runtime_profile(view, session_kind="agent_chat")

    assert profile.sandbox_tenancy == "conversation"
    # The image's own home, which exists before any session does — what lets a
    # pooled box be claimed with nothing to build.
    assert profile.home_template == "/home/agent"

    # No ConversationIdentity is passed at all, so the backend is never even
    # asked whether its boxes could carry more. That is the point: a capable
    # substrate must not start packing conversations on its own.
    assert _identity(profile, "/home/agent") is None


async def test_both_tenancies_answer_the_same_engine_questions() -> None:
    """The two documents are one function, so they cannot drift.

    As two, they did immediately: the shared one was written with no
    ``env_policy`` at all, and nothing would have caught it until a
    platform-owned variable came through from a template.
    """
    shared = resolve_runtime_profile(
        await _harness(engine_kind="claude_code", sandbox_tenancy="agent")
    )
    own = resolve_runtime_profile(await _harness(engine_kind="claude_code"))

    assert shared.config_dir_name == own.config_dir_name == ".claude"
    shared_doc = asdict(shared)
    own_doc = asdict(own)
    differ = {
        key
        for key in set(shared_doc) | set(own_doc)
        if shared_doc.get(key) != own_doc.get(key)
    }
    assert differ == {
        "sandbox_tenancy",
        "username_template",
        "home_template",
        "workspace_source_template",
        "required_commands",
    }


async def test_a_shared_workspace_engine_refuses_a_box_per_conversation() -> None:
    """Assistant workspaces reject unsupported per-conversation tenancy.

    The adapter provides one workspace per account, so accepting this setting
    would silently keep sharing the box.
    """
    view = await _harness(engine_kind="assistant", sandbox_tenancy="conversation")
    with pytest.raises(APIError) as caught:
        resolve_runtime_profile(view, session_kind="assistant_chat", engine_kind="assistant")

    assert caught.value.code == "UNSUPPORTED_SANDBOX_TENANCY"
    assert caught.value.status_code == 409


async def test_a_shared_workspace_engine_takes_the_tenancy_it_does_run() -> None:
    view = await _harness(engine_kind="assistant", sandbox_tenancy="agent")
    profile = resolve_runtime_profile(
        view, session_kind="assistant_chat", engine_kind="assistant"
    )
    assert profile.sandbox_tenancy == "agent"
    assert profile.config_dir_name == ".hermes"
    # The engine's own binary is demanded, and the platform's account-assembly
    # pair rides at the end of every shared-tenancy composition. The old
    # assertion pinned "hermes" as the LAST command, which was false even
    # against the hand-written declaration it was written for (that list
    # ended with the profile-setup script) — it had simply never been run
    # against what it claimed to pin.
    assert "hermes" in profile.required_commands
    assert profile.required_commands[-2:] == ("useradd", "groupadd")


async def test_an_unknown_tenancy_is_refused_rather_than_defaulted() -> None:
    view = await _harness(engine_kind="claude_code", sandbox_tenancy="whatever")
    with pytest.raises(APIError) as caught:
        resolve_runtime_profile(view, session_kind="agent_chat")
    assert caught.value.code == "UNSUPPORTED_SANDBOX_TENANCY"


async def test_the_form_offers_only_values_the_runtime_accepts() -> None:
    """An operator picking from a dropdown must never be told at the next
    session start that the value is not implemented."""
    row = next(
        f for f in get_environment_schema()["fields"] if f["key"] == "sandbox_tenancy"
    )
    assert row["default"] == "conversation"
    for tenancy in row["enum"]:
        view = await _harness(engine_kind="claude_code", sandbox_tenancy=tenancy)
        resolve_runtime_profile(view, session_kind="agent_chat")
