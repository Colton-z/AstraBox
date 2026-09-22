"""Provider-owned request headers in the per-conversation runtime overlay.

The bundled LiteLLM gateway's langfuse callback maps the request headers
``langfuse_session_id`` / ``langfuse_trace_user_id`` onto the Langfuse trace's
sessionId/userId. To make a Langfuse session == an AstraBox conversation (and
attribute its traces to the AstraBox user) at zero config, the orchestrator
tags the long-lived sandbox ``claude`` CLI with ``ANTHROPIC_CUSTOM_HEADERS`` at
launch. The bundled adapter supplies those headers through the model seam. A
provider that returns no headers receives none, and a user/agent-supplied
``ANTHROPIC_CUSTOM_HEADERS`` remains ahead of provider-owned values.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import astrabox.providers.model  # noqa: F401 — registers direct/litellm endpoint providers
from astrabox.common.utils.settings import (
    DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
)
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    build_claude_options_kwargs,
)
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointProvider,
    register_model_endpoint,
)


class _NoHeaderProvider(ModelEndpointProvider):
    name = "acme-gateway"

    def resolve(self, *, requested: ModelEndpoint, settings=None) -> ModelEndpoint:
        _ = settings
        return requested


register_model_endpoint(_NoHeaderProvider())

_SESSION_ID = "conv-abc123"
_USER_ID = "user-42"
_HEADER = "ANTHROPIC_CUSTOM_HEADERS"


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "remote_agent_include_partial_messages": False,
        "remote_agent_max_buffer_size": DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
        # Empty proxy base URL => the transcript-capability block is skipped, so
        # the test needs no signing key / capability plumbing.
        "mcp_proxy_base_url": "",
        "model_endpoint_provider": "acme-gateway",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _template(**overrides: object) -> SimpleNamespace:
    base = {
        "name": "corr-agent",
        "engine_options": None,
        "mcp_servers": {},
        "skills": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _identity(*, user_id: str | None) -> dict[str, object]:
    identity: dict[str, object] = {
        "linux_user": "convuser",
        "home_dir": "/home/convuser",
        "workspace_dir": "/home/convuser/workspace",
        "config_dir": "/home/convuser/.claude",
        "sandbox_tenancy": "conversation",
    }
    if user_id is not None:
        identity["user_id"] = user_id
    return identity


_GATEWAY_BASE_URL = "https://litellm.internal/v1"


def _creds(endpoint_provider: str, *, base_url: str = _GATEWAY_BASE_URL) -> dict[str, str]:
    return {
        "model_name": "claude-opus-4",
        "api_key": "sk-test",
        # A gateway config HAS an endpoint. The headers are gated on it (see
        # ``tests/model_endpoint_delivery_invariant_test.py``): the CLI attaches
        # them to requests aimed at ANTHROPIC_BASE_URL, so with no such variable
        # the destination is the vendor default and the litellm-only rule would
        # be enforced in name only.
        "base_url": base_url,
        "credential_header": "ANTHROPIC_AUTH_TOKEN",
        "endpoint_provider": endpoint_provider,
    }


def _build_env(
    *,
    endpoint_provider: str,
    user_id: str | None = _USER_ID,
    template: SimpleNamespace | None = None,
    base_url: str = _GATEWAY_BASE_URL,
) -> dict[str, str]:
    options = build_claude_options_kwargs(
        _settings(),
        template if template is not None else _template(),
        session_id=_SESSION_ID,
        runtime_identity=_identity(user_id=user_id),
        model_runtime_creds=_creds(endpoint_provider, base_url=base_url),
        permission_mode="default",
    )
    return dict(options.get("env") or {})


class LangfuseCorrelationHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        # Force local mode OFF so the debug-file branch (which needs a writable
        # config dir) stays out of the way and the non-local path is exercised.
        self._env_patch = patch.dict(os.environ, {"ASTRABOX_LOCAL_MODE": ""})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_litellm_provider_injects_both_correlation_pairs(self) -> None:
        env = _build_env(endpoint_provider="litellm")
        self.assertEqual(
            env[_HEADER],
            f"langfuse_session_id: {_SESSION_ID}, langfuse_trace_user_id: {_USER_ID}",
        )

    def test_provider_without_headers_never_injects_the_header(self) -> None:
        env = _build_env(endpoint_provider="acme-gateway")
        self.assertNotIn(_HEADER, env)

    def test_preexisting_user_header_is_preserved_and_ours_appended(self) -> None:
        user_header = "X-My-Header: mine"
        template = _template(engine_options={"sdk_options": {"env": {_HEADER: user_header}}})
        env = _build_env(endpoint_provider="litellm", template=template)
        self.assertEqual(
            env[_HEADER],
            f"{user_header}, langfuse_session_id: {_SESSION_ID}, "
            f"langfuse_trace_user_id: {_USER_ID}",
        )
        # Theirs stays first (never clobbered).
        self.assertTrue(env[_HEADER].startswith(f"{user_header}, "))

    def test_empty_user_id_emits_only_the_session_pair(self) -> None:
        env = _build_env(endpoint_provider="litellm", user_id=None)
        self.assertEqual(env[_HEADER], f"langfuse_session_id: {_SESSION_ID}")
        self.assertNotIn("langfuse_trace_user_id", env[_HEADER])

    def test_litellm_without_an_endpoint_never_injects_the_header(self) -> None:
        # The provider name alone does not make a gateway. With no
        # ANTHROPIC_BASE_URL the CLI talks to the vendor default, so emitting
        # the headers would send the conversation id and the AstraBox user id
        # to a third party — the exact leak the litellm gate exists to prevent.
        env = _build_env(endpoint_provider="litellm", base_url="")
        self.assertNotIn(_HEADER, env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)


if __name__ == "__main__":
    unittest.main()
