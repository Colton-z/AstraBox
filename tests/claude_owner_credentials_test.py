"""Claude Code on its user's own Anthropic API key, or on the login its user makes in the sandbox.

The two model endpoints resolve to Anthropic's own API. What reaches the
Claude Code CLI is the env map ``build_claude_options_kwargs`` builds from the
platform's resolved access, so the cases assert over that map: an API key
rides ``ANTHROPIC_API_KEY`` with no base URL (the CLI's own default endpoint),
and the in-box login gets no credential and no base URL at all, with every
source Claude Code ranks above a ``/login`` credential refused.

The refused-login mapping is fed the runner's own frames, serialized by
``sandbox_runner`` from the SDK's types, with the values Claude Code 2.1.308
answered with no credentials (an assistant message of model ``<synthetic>``
carrying ``error="authentication_failed"`` and the text ``Not logged in ·
Please run /login``, then a Result with ``subtype="success"``,
``is_error=True`` and ``terminal_reason="api_error"``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, UserMessage

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    ModelConfig,
    build_claude_model_config_kwargs,
    build_claude_options_kwargs,
)
from astrabox.core.service.orchestrator.engine.claude_code_runtime import (
    _anthropic_container_env,
    _model_runtime_creds,
    claude_login_hint,
)
from astrabox.core.service.orchestrator.engine.claude_runtime_client import (
    CLAUDE_LOGIN_REQUIRED,
    ClaudeRuntimeClient,
)
from astrabox.core.service.orchestrator.engine.runtime import ResponseEnded
from astrabox.core.service.orchestrator.sandbox_runner import EnvelopeSender, HistoryStoreSequence, _jsonable
from astrabox.providers.model_anthropic import (
    AnthropicAPIKeyModelEndpointProvider,
    ClaudeLoginModelEndpointProvider,
)
from astrabox.seams.egress_credentials import EGRESS_HELD_PLACEHOLDER
from astrabox.seams.model import ModelEndpoint, ResolvedModelAccess, register_model_endpoint

register_model_endpoint(AnthropicAPIKeyModelEndpointProvider())
register_model_endpoint(ClaudeLoginModelEndpointProvider())

_IDENTITY: dict[str, Any] = {
    "sandbox_tenancy": "conversation",
    "linux_user": "agent",
    "home_dir": "/home/agent",
    "workspace_dir": "/workspace",
    "config_dir": "/home/agent/.claude",
    "config_env_var": "CLAUDE_CONFIG_DIR",
}
_CREDENTIAL_ENV = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}


def _access(provider: str) -> ResolvedModelAccess:
    """What the config resolver answers for a RuntimeProfile on ``provider``."""
    endpoint = (
        AnthropicAPIKeyModelEndpointProvider()
        if provider == "anthropic_api_key"
        else ClaudeLoginModelEndpointProvider()
    ).resolve(requested=ModelEndpoint(model_name="claude-sonnet-4-5"))
    return ResolvedModelAccess(
        configuration={},
        base_url=endpoint.base_url,
        model_name=endpoint.model_name,
        credential=None,
        credential_kind=endpoint.credential_kind or "bearer",
        endpoint_provider=provider,
    )


def _options(
    provider: str,
    *,
    delivered: str,
    engine_options: dict[str, Any] | None = None,
    runtime_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The option bag for one conversation, as activation builds it."""
    model_config = ModelConfig(**build_claude_model_config_kwargs(_access(provider)))
    creds = _model_runtime_creds(model_config, vault=False)
    creds["api_key"] = delivered
    return build_claude_options_kwargs(
        SimpleNamespace(
            remote_agent_include_partial_messages=False,
            remote_agent_max_buffer_size=DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
            mcp_proxy_base_url="",
        ),
        cast(Any, SimpleNamespace(
            name="agent", engine_options=engine_options, mcp_servers={}, skills=None, system=None,
        )),
        session_id="conv-1",
        runtime_identity=dict(_IDENTITY),
        model_runtime_creds=creds,
        runtime_env=runtime_env,
        permission_mode="bypassPermissions",
    )


def test_an_owners_api_key_reaches_claude_code_as_its_own_key_at_its_default_endpoint() -> None:
    """The placeholder rides ``ANTHROPIC_API_KEY`` (sent as ``X-Api-Key``); no base URL is named.

    A named ``ANTHROPIC_BASE_URL`` makes Claude Code treat Anthropic as a
    gateway at a custom address, so the box and the CLI name none.
    """
    model_config = ModelConfig(**build_claude_model_config_kwargs(_access("anthropic_api_key")))
    assert (model_config.base_url, model_config.credential_header) == ("", "ANTHROPIC_API_KEY")
    env = _options("anthropic_api_key", delivered=EGRESS_HELD_PLACEHOLDER)["env"]
    assert env["ANTHROPIC_API_KEY"] == EGRESS_HELD_PLACEHOLDER
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in _anthropic_container_env(model_config)


def test_a_login_in_the_sandbox_gets_no_credential_and_no_endpoint() -> None:
    """Claude Code starts with nothing that outranks the login its user makes in the box."""
    env = _options("claude_login", delivered="")["env"]
    assert not (_CREDENTIAL_ENV | {"ANTHROPIC_BASE_URL"}) & set(env)
    assert env["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5"


@pytest.mark.parametrize(
    "engine_options, runtime_env, code",
    [
        ({"sdk_options": {"env": {"ANTHROPIC_API_KEY": "sk-ant-x"}}}, None, "CLAUDE_OPTIONS_INVALID"),
        ({"sdk_options": {"settings": {"apiKeyHelper": "/bin/key"}}}, None, "CLAUDE_OPTIONS_INVALID"),
        (
            {"sdk_options": {"settings": {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}}},
            None,
            "CLAUDE_OPTIONS_INVALID",
        ),
        (None, {"CLAUDE_CODE_OAUTH_TOKEN": "astrabox-placeholder"}, "VAULT_CREDENTIAL_CONFLICT"),
    ],
)
def test_nothing_may_outrank_the_login_made_in_the_sandbox(
    engine_options: dict[str, Any] | None, runtime_env: dict[str, str] | None, code: str
) -> None:
    """Each of these would run the conversation on another account without a word."""
    with pytest.raises(APIError) as refused:
        _options(
            "claude_login", delivered="", engine_options=engine_options, runtime_env=runtime_env
        )
    assert refused.value.code == code


class _Sink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


class _Link:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = frames
        self.protocol_fault: str | None = None

    @property
    def is_live(self) -> bool:
        return True

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        for frame in self._frames:
            yield frame


async def _unauthenticated_turn() -> list[dict[str, Any]]:
    """One turn as the runner journals it when Claude Code has no login."""
    return await _failed_turn("Not logged in · Please run /login", "authentication_failed", None)


async def _failed_turn(said: str, error: str, status: int | None) -> list[dict[str, Any]]:
    """One turn the model API refused, as the runner journals it."""
    sink = _Sink()
    sender = EnvelopeSender(sink, "conv-1")
    await sender.send(
        "event",
        message_type="UserMessage",
        message=_jsonable(UserMessage(content="Hello", uuid=str(uuid.uuid4()))),
        requires_persistence=True,
    )
    await sender.send(
        "event",
        message_type="AssistantMessage",
        message=_jsonable(AssistantMessage(
            content=[TextBlock(text=said)], model="<synthetic>", session_id="native-1",
            error=cast(Any, error), uuid=str(uuid.uuid4()),
        )),
        requires_persistence=True,
    )
    await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(1),
        message_type="ResultMessage",
        message=_jsonable(ResultMessage(
            subtype="success", duration_ms=267, duration_api_ms=0, is_error=True, num_turns=1,
            session_id="native-1", result=said, terminal_reason="api_error",
            api_error_status=status, uuid=str(uuid.uuid4()),
        )),
        requires_persistence=True,
    )
    return sink.frames


async def _ended(frames: list[dict[str, Any]], hint: str | None, monkeypatch: pytest.MonkeyPatch) -> ResponseEnded:
    async def no_child_agents(client: ClaudeRuntimeClient) -> None:
        client._children_restored = True

    monkeypatch.setattr(ClaudeRuntimeClient, "_restore_children", no_child_agents)
    client = ClaudeRuntimeClient(
        cast(Any, _Link(frames)), session_id="conv-1", hello={"boot_id": "boot-1"},
        resume_session_key=None, sign_in_hint=hint,
    )
    [ended] = [event async for event in client.events(after=None) if isinstance(event, ResponseEnded)]
    return ended


async def test_a_response_refused_for_want_of_a_login_says_how_to_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first response before any sign-in fails with the stable code and the terminal recipe.

    The same run on a gateway keeps Claude Code's own verdict: there the user
    cannot fix a refused gateway credential from the terminal.
    """
    frames = await _unauthenticated_turn()
    hint = claude_login_hint(_IDENTITY)

    ended = await _ended(frames, hint, monkeypatch)
    assert ended.outcome == "failed"
    assert ended.error is not None and ended.error["code"] == CLAUDE_LOGIN_REQUIRED
    message = ended.error["message"]
    assert message.startswith(hint) and message.endswith("(Claude Code: Not logged in · Please run /login)")
    assert "runuser -u agent -- env HOME=/home/agent CLAUDE_CONFIG_DIR=/home/agent/.claude claude auth login" in message

    gateway = await _ended(frames, None, monkeypatch)
    assert gateway.error is not None and gateway.error["code"] == "api_error"


async def test_a_rate_limited_claude_response_holds_no_model_account_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the Codex adapter reads a plan usage limit; a 429 from Anthropic is not one.

    The team pool holds an account back for an hour when an adapter declares
    ``usage_limited``. An Anthropic key's rate limit is not a ChatGPT plan's
    usage limit, so the Claude adapter never declares it, on either endpoint.
    """
    frames = await _failed_turn("API Error: 429 rate_limit_error", "rate_limit", 429)
    for hint in (claude_login_hint(_IDENTITY), None):
        ended = await _ended(frames, hint, monkeypatch)
        assert (ended.outcome, ended.usage_limited) == ("failed", False)
        assert ended.error is not None and ended.error["code"] == "api_error"
