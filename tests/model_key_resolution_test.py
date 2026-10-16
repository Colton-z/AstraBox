"""Model access resolution — the bring-your-own-key contract.

The plaintext gate (local mode / ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY)
applies ONLY to a plaintext key stored in the template's model_config (user
content in the metadata store). Operator-supplied secrets — env vars and the
settings file — are the deployment's secret channel and must work with NO extra
mode flags: `ANTHROPIC_API_KEY` (bridged to settings.model_api_key) or
`ASTRABOX_MODEL_API_KEY` alone yields a working key resolution.

These cases pin the gate on template plaintext and that the resolved payload
never carries the key.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import astrabox.providers.model  # noqa: F401 — registers the litellm endpoint provider
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


    def test_resolved_payload_never_carries_the_key(self) -> None:
        os.environ["ASTRABOX_MODEL_API_KEY"] = "sk-from-env"
        resolver = RuntimeConfigResolver(_settings(model_api_key="sk-from-settings"))
        payload = resolver.resolve_model_config({"model_name": "claude-opus-4-8"})
        self.assertNotIn("api_key", payload)

        os.environ["ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY"] = "1"
        access = resolver.resolve_model_access({"api_key": "stored-secret"})
        self.assertEqual(access.credential, "stored-secret")
        self.assertNotIn("api_key", access.configuration)


if __name__ == "__main__":
    unittest.main()
