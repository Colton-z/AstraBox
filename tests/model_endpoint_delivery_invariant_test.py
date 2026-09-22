"""THE INVARIANT: a gateway credential never travels without its endpoint.

``options.env`` is the only env channel that reaches EVERY box. A cold-created
box also gets a container env at create time (``_anthropic_container_env``), but
a PREWARMED box is created before any session claims it, so it has no
per-session container env at all — whatever ``build_claude_options`` puts in
``options.env`` is the whole of what its ``claude`` CLI ever sees.

There are therefore TWO env maps aimed at one box, and this file pins the
invariant on both, separately and merged:

* ``_anthropic_container_env`` (sandbox-create env) must carry NO credential at
  all. A second credential channel would have to duplicate the endpoint-pairing
  rule enforced by ``options.env`` and could send a gateway credential to the
  vendor default when the endpoint is absent.
* ``options.env`` carries the credential, and only together with its endpoint.

The effective CLI view is the identity-free create env inherited by the runner,
overlaid by the SDK with ``options.env`` for that CLI child. The backend is also
checked separately to ensure it does not copy the latter onto the box.

That makes one property load-bearing rather than incidental:

    if the CLI is handed ``ANTHROPIC_AUTH_TOKEN`` or ``ANTHROPIC_CUSTOM_HEADERS``,
    it must be handed ``ANTHROPIC_BASE_URL`` in the SAME map.

``ANTHROPIC_AUTH_TOKEN`` is a Bearer credential minted by a gateway, and
``ANTHROPIC_CUSTOM_HEADERS`` carries the Langfuse correlation pair naming the
conversation and the AstraBox user. The CLI attaches both to every request it
sends to ``ANTHROPIC_BASE_URL`` — so if that variable is missing, the CLI falls
back to the vendor default and posts a third party's credential, plus
AstraBox's session and user ids, to Anthropic.

``ANTHROPIC_API_KEY`` is deliberately NOT covered: an x-api-key vendor key with
no base URL is addressed to the vendor default, which is exactly where it
belongs.

These tests assert the property over the ENV MAP, not over the implementation,
so they keep holding if the delivery is refactored — and fail if the endpoint is
ever moved back onto a channel a pooled box does not have.
"""

from __future__ import annotations

import unittest
from typing import Any
from types import SimpleNamespace

from claude_agent_sdk import ClaudeAgentOptions

import astrabox.providers.model  # noqa: F401 — registers direct/litellm endpoint providers
from astrabox.common.utils.settings import (
    DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
)
from astrabox.core.service.orchestrator.engine.claude_code_runtime import (
    _anthropic_container_env,
    _model_runtime_creds,
)
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    build_claude_options_kwargs,
)
from astrabox.providers.sandbox_image import SANDBOX_SELF_DESCRIPTION
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointProvider,
    register_model_endpoint,
)


class _PassthroughEndpointProvider(ModelEndpointProvider):
    def __init__(self, name: str) -> None:
        self.name = name

    def resolve(self, *, requested: ModelEndpoint, settings=None) -> ModelEndpoint:
        _ = settings
        return requested


register_model_endpoint(_PassthroughEndpointProvider("acme-passthrough"))
register_model_endpoint(_PassthroughEndpointProvider("direct"))

_BASE_URL_KEY = "ANTHROPIC_BASE_URL"
#: Keys that are meaningless — and dangerous — without a base URL to aim them at.
_ENDPOINT_BOUND_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS")

_SESSION_ID = "conv-invariant-1"
_USER_ID = "user-invariant-1"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        remote_agent_include_partial_messages=False,
        remote_agent_max_buffer_size=DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
        # Empty proxy base URL => the transcript-capability block is skipped, so
        # these tests need no signing key / capability plumbing.
        mcp_proxy_base_url="",
        model_endpoint_provider="acme-passthrough",
    )


def _template() -> SimpleNamespace:
    return SimpleNamespace(
        name="invariant-agent", engine_options=None, mcp_servers={}, skills=None
    )


def _identity() -> dict[str, object]:
    return {
        "linux_user": "convuser",
        "home_dir": "/home/convuser",
        "workspace_dir": "/home/convuser/workspace",
        "config_dir": "/home/convuser/.claude",
        "user_id": _USER_ID,
        "sandbox_tenancy": "conversation",
    }


def _model_config(**overrides: object) -> SimpleNamespace:
    base = dict(
        base_url="https://gateway.internal/v1",
        api_key="sk-the-credential",
        model_name="claude-opus-4",
        mcp_token="",
        credential_header="ANTHROPIC_AUTH_TOKEN",
        endpoint_provider="litellm",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


#: Every endpoint/credential shape a deployment can actually produce. Shared by
#: both injection points so neither is pinned on a narrower matrix than the other.
_CASES: dict[str, SimpleNamespace] = {
    "litellm gateway": _model_config(),
    "litellm gateway, no credential": _model_config(api_key=""),
    "passthrough plugin vendor, x-api-key": _model_config(
        base_url="", credential_header="ANTHROPIC_API_KEY", endpoint_provider="acme-passthrough"
    ),
    "passthrough plugin vendor with explicit endpoint": _model_config(
        base_url="https://api.anthropic.com",
        credential_header="ANTHROPIC_API_KEY",
        endpoint_provider="acme-passthrough",
    ),
    "bearer token but NO endpoint": _model_config(base_url=""),
    "litellm provider but NO endpoint": _model_config(base_url="", endpoint_provider="litellm"),
    "no endpoint, no credential": _model_config(base_url="", api_key=""),
}


def _create_env(model_config: Any) -> dict[str, str]:
    """The box's boot environment, composed the way the platform composes it.

    The deployment facts every AstraBox sandbox carries, plus the engine's
    non-secret model settings. It is the platform that puts these together —
    the seam writes `env` verbatim — so this is where the invariant has to be
    asked.
    """

    return {**SANDBOX_SELF_DESCRIPTION, **_container_env(model_config)}


def _cli_env(model_config: SimpleNamespace) -> dict[str, str]:
    """The env map the in-box ``claude`` CLI is actually launched with.

    Goes through the real production path — ``_model_runtime_creds`` (what the
    engine forwards) into ``build_claude_options`` (what builds the overlay) —
    so a regression in EITHER half is caught here.
    """
    options = build_claude_options_kwargs(
        _settings(),
        _template(),
        session_id=_SESSION_ID,
        runtime_identity=_identity(),
        model_runtime_creds=_model_runtime_creds(model_config),
        permission_mode="default",
    )
    return dict(options.get("env") or {})


def _container_env(model_config: SimpleNamespace) -> dict[str, str]:
    """The env the box is CREATED with (a cold box only; a pooled one gets none)."""
    return _anthropic_container_env(model_config)


def _box_env(model_config: SimpleNamespace) -> dict[str, str]:
    """Everything a cold-created box's CLI can see after the SDK overlay.

    The runner inherits the create env; the SDK then applies ``options.env`` to
    the CLI child. Process-local values therefore win without becoming sandbox
    creation state.
    """
    cli_env = _cli_env(model_config)
    return {**_create_env(model_config), **cli_env}


class ModelEndpointDeliveryInvariantTests(unittest.TestCase):
    def _assert_invariant(self, env: dict[str, str], *, case: str) -> None:
        for key in _ENDPOINT_BOUND_KEYS:
            if env.get(key):
                self.assertTrue(
                    env.get(_BASE_URL_KEY),
                    f"{case}: the CLI env carries {key} without {_BASE_URL_KEY}; "
                    "the CLI would send it to the vendor default instead of the "
                    "gateway it belongs to",
                )

    def test_gateway_config_delivers_endpoint_alongside_credential(self) -> None:
        env = _cli_env(_model_config())
        # Guard against the test passing vacuously: this config MUST produce the
        # endpoint-bound keys, or it is not exercising the invariant at all.
        self.assertEqual(env[_BASE_URL_KEY], "https://gateway.internal/v1")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-the-credential")
        self.assertIn("ANTHROPIC_CUSTOM_HEADERS", env)
        self.assertIn(_SESSION_ID, env["ANTHROPIC_CUSTOM_HEADERS"])
        self._assert_invariant(env, case="litellm gateway")

    def test_invariant_holds_across_every_endpoint_shape(self) -> None:
        # The matrix a deployment can actually produce. None of these may leak a
        # Bearer token or the correlation headers toward the vendor default.
        for case, model_config in _CASES.items():
            with self.subTest(case=case):
                self._assert_invariant(_cli_env(model_config), case=case)

    def test_bearer_credential_is_withheld_when_there_is_no_gateway(self) -> None:
        # Fail closed: a gateway Bearer token with no gateway to send it to is
        # dropped, not redirected to the vendor default.
        env = _cli_env(_model_config(base_url=""))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn(_BASE_URL_KEY, env)
        # The model name is not a secret and still travels, so the failure the
        # operator sees is "no auth", not "no model".
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-opus-4")

    def test_vendor_api_key_still_travels_without_a_base_url(self) -> None:
        # The exemption: x-api-key against the vendor default is correct, and
        # must not be collateral damage of the rule above.
        env = _cli_env(
            _model_config(
                base_url="", credential_header="ANTHROPIC_API_KEY", endpoint_provider="direct"
            )
        )
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-the-credential")
        self.assertNotIn(_BASE_URL_KEY, env)

    def test_correlation_headers_need_an_endpoint_not_just_a_provider(self) -> None:
        # endpoint_provider=="litellm" alone must not arm the headers: without a
        # base URL the CLI's destination is the vendor, and "litellm-only" would
        # be enforced in name only.
        env = _cli_env(_model_config(base_url="", endpoint_provider="litellm"))
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", env)

    def test_endpoint_rides_the_channel_a_pooled_box_can_receive(self) -> None:
        # The prewarm-specific half of the invariant, pinned at its source: a
        # pooled box gets no container env, so the endpoint has to be in the
        # per-conversation creds the engine forwards. Reading it out of
        # _model_runtime_creds is what makes "options.env carries it" structural
        # rather than a coincidence of build_claude_options.
        creds = _model_runtime_creds(_model_config())
        self.assertEqual(creds["base_url"], "https://gateway.internal/v1")


class ContainerEnvInjectionPointTests(unittest.TestCase):
    """The second injection point: the env a cold box is CREATED with."""

    def test_the_container_env_carries_no_credential_in_any_shape(self) -> None:
        # The gate on options.env cannot protect a credential that a second
        # channel delivers on its own. This one delivers none, so there is
        # nothing to gate twice.
        for case, model_config in _CASES.items():
            with self.subTest(case=case):
                env = _container_env(model_config)
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env, case)
                self.assertNotIn("ANTHROPIC_API_KEY", env, case)
                self.assertNotIn(
                    "sk-the-credential",
                    env.values(),
                    f"{case}: the create-time env carries the credential under "
                    "some other name",
                )

    def test_the_container_env_still_addresses_the_model(self) -> None:
        # Non-vacuity, and the reason this map still exists: the box knows what
        # it was talking to even though it does not know how to authenticate.
        env = _container_env(_model_config())
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gateway.internal/v1")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-opus-4")

    def test_backend_does_not_stamp_cli_identity_or_credentials_on_a_cold_box(
        self,
    ) -> None:
        cli_env = _cli_env(_model_config())

        create_env = _create_env(_model_config())
        for key in (
            "HOME",
            "USER",
            "LOGNAME",
            "PWD",
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_AUTH_TOKEN",
        ):
            self.assertNotIn(key, create_env)
        self.assertEqual(create_env["ANTHROPIC_BASE_URL"], "https://gateway.internal/v1")


class EffectiveBoxEnvInvariantTests(unittest.TestCase):
    """Both maps merged the way the backend merges them, for one cold box."""

    def test_the_invariant_holds_over_the_merged_env(self) -> None:
        for case, model_config in _CASES.items():
            with self.subTest(case=case):
                env = _box_env(model_config)
                for key in _ENDPOINT_BOUND_KEYS:
                    if env.get(key):
                        self.assertTrue(
                            env.get(_BASE_URL_KEY),
                            f"{case}: the box env carries {key} without "
                            f"{_BASE_URL_KEY}",
                        )

    def test_a_cold_box_is_still_fully_configured(self) -> None:
        # Guard against the invariant being satisfied by delivering nothing:
        # the gateway case must still hand the box its endpoint AND its token.
        env = _box_env(_model_config())
        self.assertEqual(env[_BASE_URL_KEY], "https://gateway.internal/v1")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-the-credential")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-opus-4")

    def test_a_bearer_token_with_no_gateway_reaches_the_box_on_neither_channel(
        self,
    ) -> None:
        # The whole point of removing the credential from the create-time env:
        # the withheld credential stays withheld in the merged view too.
        env = _box_env(_model_config(base_url=""))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn(_BASE_URL_KEY, env)
        self.assertNotIn("sk-the-credential", env.values())


if __name__ == "__main__":
    unittest.main()
