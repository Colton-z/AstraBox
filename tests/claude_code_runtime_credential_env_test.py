"""Resolved model credentials use the selected header and delivery channel.

``ModelConfig.credential_header`` is mapped by the Claude adapter from the
neutral model-access credential kind; the source matrix is covered in
``tests/model_key_resolution_test.py``. It selects either
``ANTHROPIC_API_KEY`` x-api-key semantics or ``ANTHROPIC_AUTH_TOKEN`` Bearer
semantics for the agent-process environment.

``claude_code_runtime`` builds TWO env maps for a box, and only ONE of them is
allowed to carry a credential:

* ``_anthropic_container_env`` — the sandbox-create-time env. Endpoint, model
  name, ``IS_SANDBOX``; no credential, under any configuration.
* ``_model_runtime_creds`` — the per-conversation overlay
  ``build_claude_options`` turns into ``options.env``. This one carries the
  credential, its header, and the endpoint it is scoped to.

That split enforces the fail-closed rule that a gateway Bearer token without a
``base_url`` is withheld. A prewarmed box receives no per-session container env,
so credentials must travel through the per-conversation overlay.
``tests/model_endpoint_delivery_invariant_test.py`` pins the resulting property
over both maps together.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrabox.common.utils.settings import (
    DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
)
from astrabox.core.service.orchestrator.engine.claude_code_runtime import (
    _anthropic_container_env,
    _credential_header,
    _model_runtime_creds,
)
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    build_claude_options_kwargs,
)

_CREDENTIAL = "sk-the-credential"
_CREDENTIAL_HEADERS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _model_config(**overrides: object) -> SimpleNamespace:
    base = dict(
        base_url="https://api.anthropic.com",
        api_key=_CREDENTIAL,
        model_name="claude-opus-4",
        mcp_token="",
        credential_header="ANTHROPIC_AUTH_TOKEN",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class ContainerEnvCarriesNoCredentialTests(unittest.TestCase):
    """The sandbox-create env is credential-free for every model config."""

    def test_no_credential_under_either_header(self) -> None:
        for header in _CREDENTIAL_HEADERS:
            with self.subTest(credential_header=header):
                env = _anthropic_container_env(_model_config(credential_header=header))
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_no_credential_value_anywhere_in_the_map(self) -> None:
        # Stronger than naming the two headers: the secret must not appear under
        # ANY key, including one a future edit invents.
        for model_config in (
            _model_config(),
            _model_config(credential_header="ANTHROPIC_API_KEY"),
            # A credential with no endpoint: the two conditions must be checked
            # together, or the token goes in while the CLI falls back to the
            # vendor default to spend it.
            _model_config(base_url=""),
            _model_config(base_url="", credential_header="X-Something-Weird"),
        ):
            with self.subTest(base_url=model_config.base_url):
                env = _anthropic_container_env(model_config)
                self.assertNotIn(_CREDENTIAL, env.values())

    def test_the_non_secret_model_address_still_travels(self) -> None:
        # Withholding the credential must not blank the box's self-description:
        # the endpoint and model name are what a diagnostic report is read for.
        env = _anthropic_container_env(_model_config(base_url="https://gw.internal/v1"))
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gw.internal/v1")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-opus-4")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "claude-opus-4")
        self.assertEqual(env["IS_SANDBOX"], "1")

    def test_an_absent_endpoint_is_simply_absent(self) -> None:
        env = _anthropic_container_env(_model_config(base_url=""))
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(env["IS_SANDBOX"], "1")


class ModelRuntimeCredsCarriesHeaderTests(unittest.TestCase):
    def test_credential_header_travels_with_the_per_conversation_creds(self) -> None:
        # _model_runtime_creds feeds build_claude_options' per-conversation
        # options.env overlay — the one channel that delivers the credential —
        # so the header the source resolved must ride along with it.
        for header in _CREDENTIAL_HEADERS:
            model_config = _model_config(credential_header=header)
            creds = _model_runtime_creds(model_config)
            self.assertEqual(creds["credential_header"], header)
            self.assertEqual(creds["api_key"], _CREDENTIAL)

    def test_missing_credential_header_attr_defaults_to_bearer(self) -> None:
        # A ModelConfig without credential_header uses the documented Bearer
        # default rather than inventing an environment variable name.
        model_config = SimpleNamespace(
            base_url="", api_key=_CREDENTIAL, model_name="", mcp_token=""
        )
        creds = _model_runtime_creds(model_config)
        self.assertEqual(creds["credential_header"], "ANTHROPIC_AUTH_TOKEN")

    def test_garbage_credential_header_value_falls_back_to_bearer(self) -> None:
        # Fail-safe, not fail-loud: an unrecognized header name never invents a
        # third sandbox env var for the credential to ride under.
        creds = _model_runtime_creds(_model_config(credential_header="X-Something-Weird"))
        self.assertEqual(creds["credential_header"], "ANTHROPIC_AUTH_TOKEN")

    def test_credential_header_helper_is_the_single_resolver(self) -> None:
        for header in _CREDENTIAL_HEADERS:
            model_config = _model_config(credential_header=header)
            self.assertEqual(_credential_header(model_config), header)
            self.assertEqual(_model_runtime_creds(model_config)["credential_header"], header)

    def test_endpoint_provider_travels_with_the_per_conversation_creds(self) -> None:
        # build_claude_options gates the Langfuse correlation headers on the
        # resolved provider, which must ride the same options.env creds dict.
        creds = _model_runtime_creds(_model_config(endpoint_provider="litellm"))
        self.assertEqual(creds["endpoint_provider"], "litellm")

    def test_missing_endpoint_provider_attr_is_empty_string(self) -> None:
        # A ModelConfig without endpoint_provider resolves to the empty provider,
        # which leaves the LiteLLM correlation-header gate closed.
        model_config = SimpleNamespace(
            base_url="", api_key=_CREDENTIAL, model_name="", mcp_token=""
        )
        self.assertEqual(_model_runtime_creds(model_config)["endpoint_provider"], "")


class ManagedCredentialEnvDeliveryTests(unittest.TestCase):
    def test_deleted_upstream_relay_has_no_inert_runtime_environment(self) -> None:
        settings = SimpleNamespace(
            remote_agent_include_partial_messages=False,
            remote_agent_max_buffer_size=DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
            mcp_proxy_base_url="https://astrabox.example",
        )
        template = SimpleNamespace(
            name="managed-agent",
            engine_options=None,
            mcp_servers={},
            plugin_repos=[],
            skills=None,
        )
        options = build_claude_options_kwargs(
            settings,
            template,
            session_id="session-1",
            permission_mode="default",
        )

        # Neither the proxy base URL nor the session id reaches the box:
        # the shim that read them is gone, and an exported variable no
        # process reads is the inert knob the contract forbids.
        env = options.get("env", {})
        self.assertNotIn("ASTRABOX_MCP_PROXY_BASE_URL", env)
        self.assertNotIn("RUNTIME_IDENTITY_SESSION_ID", env)

    def test_bound_vault_placeholder_reaches_the_agent_process_env(self) -> None:
        placeholder = "ASTRABOX-VAULT-CRED::vcr-test::nonce"
        settings = SimpleNamespace(
            remote_agent_include_partial_messages=False,
            remote_agent_max_buffer_size=DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
            mcp_proxy_base_url="",
        )
        template = SimpleNamespace(
            name="managed-agent",
            engine_options=None,
            mcp_servers={},
            skills=None,
        )
        options = build_claude_options_kwargs(
            settings,
            template,
            runtime_env={"E2E_BOUND_TOKEN": placeholder},
            permission_mode="default",
        )

        self.assertEqual(options["env"]["E2E_BOUND_TOKEN"], placeholder)


if __name__ == "__main__":
    unittest.main()
