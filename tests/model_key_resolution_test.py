"""Model access resolution — the bring-your-own-key contract.

The plaintext gate (local mode / ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY)
applies ONLY to a plaintext key stored in the template's model_config (user
content in the metadata store). Operator-supplied secrets — env vars and the
settings file — are the deployment's secret channel and must work with NO extra
mode flags: `ANTHROPIC_API_KEY` (bridged to settings.model_api_key) or
`ASTRABOX_MODEL_API_KEY` alone yields a working key resolution.

The matrix covers operator env and settings keys with the plaintext gate closed,
gated template plaintext, source precedence, and the transport-neutral
credential kind required by each winning source.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import astrabox.providers.model  # noqa: F401 — registers the litellm endpoint provider
from astrabox.common.utils.secrets import SecretProvider
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    RuntimeConfigResolver,
)
from astrabox.seams.model import ModelEndpoint, register_model_endpoint


class _PassthroughEndpoint:
    """A gateway that declines every override — the seam permits it (non-None
    fields win, all-None leaves the resolution chain in charge). These tests
    exercise the CHAIN itself, so they pin the provider to this stub instead of
    the litellm default, whose overrides would mask what is under test."""

    name = "test-passthrough"

    def resolve(self, *, requested, settings=None):  # noqa: ANN001, ANN201
        return ModelEndpoint()

    def list_models(self, *, provider_access=None, settings=None):  # noqa: ANN001, ANN201
        return []


register_model_endpoint(_PassthroughEndpoint())

_GATE_ENVS = ("ASTRABOX_LOCAL_MODE", "ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY")


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "model_api_key": "",
        "model_api_key_secret_name": "",
        "model_base_url": "",
        "model_name": "",
        "model_endpoint_provider": "test-passthrough",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class ModelKeyResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        # Force the gate CLOSED (the non-local default a fresh Docker run has).
        self._env_patch = patch.dict(
            os.environ,
            {name: "" for name in _GATE_ENVS} | {"ASTRABOX_MODEL_API_KEY": ""},
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_operator_env_key_works_without_local_mode(self) -> None:
        os.environ["ASTRABOX_MODEL_API_KEY"] = "sk-from-env"
        resolver = RuntimeConfigResolver(_settings())
        self.assertEqual(resolver.resolve_model_access({}).credential, "sk-from-env")

    def test_settings_key_works_without_local_mode(self) -> None:
        # settings.model_api_key is where ANTHROPIC_API_KEY lands via the
        # dotted alias bridge (astrabox.model.api_key -> llm_auth_token).
        resolver = RuntimeConfigResolver(_settings(model_api_key="sk-from-settings"))
        self.assertEqual(
            resolver.resolve_model_access({}).credential, "sk-from-settings"
        )

    def test_template_plaintext_key_stays_gated(self) -> None:
        resolver = RuntimeConfigResolver(_settings())
        # Stored template plaintext without the gate -> ignored (None).
        self.assertIsNone(
            resolver.resolve_model_access({"api_key": "sk-in-template"}).credential
        )
        # With the gate open it is honored.
        os.environ["ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY"] = "1"
        self.assertEqual(
            resolver.resolve_model_access(
                {"api_key": "sk-in-template"}
            ).credential,
            "sk-in-template",
        )

    def test_template_plaintext_gated_falls_back_to_operator_env(self) -> None:
        os.environ["ASTRABOX_MODEL_API_KEY"] = "sk-from-env"
        resolver = RuntimeConfigResolver(_settings())
        self.assertEqual(
            resolver.resolve_model_access(
                {"api_key": "sk-in-template"}
            ).credential,
            "sk-from-env",
        )

    def test_resolved_payload_never_carries_the_key(self) -> None:
        os.environ["ASTRABOX_MODEL_API_KEY"] = "sk-from-env"
        resolver = RuntimeConfigResolver(_settings(model_api_key="sk-from-settings"))
        payload = resolver.resolve_model_config({"model_name": "claude-opus-4-8"})
        self.assertNotIn("api_key", payload)

        os.environ["ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY"] = "1"
        access = resolver.resolve_model_access({"api_key": "stored-secret"})
        self.assertEqual(access.credential, "stored-secret")
        self.assertNotIn("api_key", access.configuration)


# ── credential-kind mapping (ANTHROPIC_API_KEY vs bearer token) ──
#
# `settings.model_api_key` bridges ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY, and
# ASTRABOX_LLM_AUTH_TOKEN into one value using AliasChoices order.
# The neutral access value mirrors that precedence against the raw env so the
# credential and its kind describe the same winning source. The Claude adapter
# maps api_key to x-api-key semantics and bearer to Authorization semantics.
_CREDENTIAL_HEADER_ENVS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ASTRABOX_LLM_AUTH_TOKEN",
    "ASTRABOX_MODEL_API_KEY",
    "ASTRABOX_MODEL_API_KEY_SECRET_NAME",
)


class CredentialHeaderMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ, {name: "" for name in _CREDENTIAL_HEADER_ENVS}
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_bare_anthropic_api_key_maps_to_x_api_key_header(self) -> None:
        # The documented ANTHROPIC_API_KEY source must retain x-api-key header
        # semantics when delivered to the runtime.
        os.environ["ANTHROPIC_API_KEY"] = "sk-plain-key"
        resolver = RuntimeConfigResolver(_settings(model_api_key="sk-plain-key"))
        access = resolver.resolve_model_access({})
        self.assertEqual(access.credential, "sk-plain-key")
        self.assertEqual(access.credential_kind, "api_key")

    def test_bare_anthropic_auth_token_maps_to_bearer_header(self) -> None:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "oauth-bearer-token"
        resolver = RuntimeConfigResolver(_settings(model_api_key="oauth-bearer-token"))
        access = resolver.resolve_model_access({})
        self.assertEqual(access.credential, "oauth-bearer-token")
        self.assertEqual(access.credential_kind, "bearer")

    def test_both_set_bearer_wins_and_matches_the_resolved_value(self) -> None:
        # Both-set case: ANTHROPIC_AUTH_TOKEN takes precedence (matching
        # AstraBoxSettings.llm_auth_token's own AliasChoices order), so the
        # header must match — never silently swapped to ANTHROPIC_API_KEY,
        # and the ANTHROPIC_API_KEY value is not silently used instead.
        os.environ["ANTHROPIC_API_KEY"] = "sk-plain-key"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "oauth-bearer-token"
        resolver = RuntimeConfigResolver(_settings(model_api_key="oauth-bearer-token"))
        access = resolver.resolve_model_access({})
        self.assertEqual(access.credential, "oauth-bearer-token")
        self.assertEqual(access.credential_kind, "bearer")

    def test_astrabox_llm_auth_token_alias_maps_to_bearer_header(self) -> None:
        os.environ["ASTRABOX_LLM_AUTH_TOKEN"] = "gw-token"
        resolver = RuntimeConfigResolver(_settings(model_api_key="gw-token"))
        self.assertEqual(resolver.resolve_model_access({}).credential_kind, "bearer")

    def test_api_key_wins_over_llm_alias_and_keeps_x_api_key_header(self) -> None:
        # ANTHROPIC_API_KEY + ASTRABOX_LLM_AUTH_TOKEN both set (no
        # ANTHROPIC_AUTH_TOKEN): AliasChoices resolves the VALUE to
        # ANTHROPIC_API_KEY's (it precedes the alias in declared order), so the
        # header must be x-api-key too. A presence-based re-derivation that
        # required the alias to be unset sent the real Anthropic key as a
        # Bearer header here — 401 on every turn against api.anthropic.com.
        os.environ["ANTHROPIC_API_KEY"] = "sk-plain-key"
        os.environ["ASTRABOX_LLM_AUTH_TOKEN"] = "legacy-fallback-value"
        resolver = RuntimeConfigResolver(_settings(model_api_key="sk-plain-key"))
        access = resolver.resolve_model_access({})
        self.assertEqual(access.credential, "sk-plain-key")
        self.assertEqual(access.credential_kind, "api_key")

    def test_settings_key_with_no_raw_env_signal_defaults_to_bearer(self) -> None:
        # A TOML/YAML-sourced settings.model_api_key has no raw header signal,
        # so it uses the Bearer default.
        resolver = RuntimeConfigResolver(_settings(model_api_key="toml-sourced-key"))
        self.assertEqual(resolver.resolve_model_access({}).credential_kind, "bearer")

    def test_higher_precedence_source_is_not_fooled_by_a_bystander_api_key_env(self) -> None:
        # ASTRABOX_MODEL_API_KEY outranks the settings bridge in
        # the model-access source order; a bystander ANTHROPIC_API_KEY
        # env (which did NOT actually win) must not flip the header.
        os.environ["ASTRABOX_MODEL_API_KEY"] = "generic-operator-token"
        os.environ["ANTHROPIC_API_KEY"] = "sk-plain-key"
        resolver = RuntimeConfigResolver(_settings(model_api_key="sk-plain-key"))
        access = resolver.resolve_model_access({})
        self.assertEqual(access.credential, "generic-operator-token")
        self.assertEqual(access.credential_kind, "bearer")

    def test_template_secret_ref_is_bearer_regardless_of_api_key_env(self) -> None:
        # A template api_key_secret_name (Vault/secret-manager reference)
        # outranks the settings bridge and is opaque bearer material — a
        # bystander ANTHROPIC_API_KEY env must not flip it to x-api-key.
        os.environ["ANTHROPIC_API_KEY"] = "sk-plain-key"
        with patch.object(SecretProvider, "get_secret", return_value="vault-secret-value"):
            resolver = RuntimeConfigResolver(_settings(model_api_key="sk-plain-key"))
            access = resolver.resolve_model_access(
                {"api_key_secret_name": "vault/ref"}
            )
            self.assertEqual(access.credential, "vault-secret-value")
            self.assertEqual(access.credential_kind, "bearer")

    def test_no_credential_configured_returns_none_with_default_header(self) -> None:
        resolver = RuntimeConfigResolver(_settings())
        access = resolver.resolve_model_access({})
        self.assertIsNone(access.credential)
        self.assertEqual(access.credential_kind, "bearer")


if __name__ == "__main__":
    unittest.main()
