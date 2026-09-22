"""Hermes assistant-engine plugin configuration.

The engine core knows no plugin by name. A memory provider (OpenViking, or any
other) is pure config under a template's ``model_config.hermes.plugins``; the
engine passes it through generically with the conversation's runtime identity
substituted. These tests pin that contract:

* a plugin's env / config_files / config are aggregated and runtime-substituted;
* no plugins → a clean empty passthrough and the platform-base config defaults
  (no memory provider forced);
* the base image / engine ship nothing provider-specific.
"""

from __future__ import annotations

import unittest

import astrabox.core.service.orchestrator.engine.hermes as h


_RUNTIME_VARS = {"conversation_user_id": "alice", "assistant_id": "asst-7", "user_id": "owner-1"}


def _openviking_plugin_template() -> dict:
    """The OpenViking memory provider expressed entirely as template config —
    the exact shape a deployment authors; nothing here is in engine code."""
    return {
        "hermes": {
            "plugins": [
                {
                    "name": "openviking",
                    "env": {
                        "OPENVIKING_ENDPOINT": "https://viking.example.com",
                        "OPENVIKING_USER": "{conversation_user_id}",
                        "OPENVIKING_ACCOUNT": "{conversation_user_id}",
                    },
                    "config_files": {
                        ".openviking/ov.conf": {
                            "bot": {
                                "ov_server": {
                                    "server_url": "https://viking.example.com",
                                    "root_api_key": "astrabox-ip-trust",
                                    "account_id": "{conversation_user_id}",
                                    "admin_user_id": "{conversation_user_id}",
                                    "agent_id": "{assistant_id}",
                                }
                            }
                        }
                    },
                    "config": {"memory": {"provider": "openviking"}},
                }
            ]
        }
    }


class HermesPluginsTest(unittest.TestCase):
    def test_plugin_env_and_files_are_runtime_substituted(self) -> None:
        plugins = h._build_hermes_plugins(_openviking_plugin_template(), runtime_vars=_RUNTIME_VARS)
        self.assertEqual(plugins.env["OPENVIKING_USER"], "alice")
        self.assertEqual(plugins.env["OPENVIKING_ACCOUNT"], "alice")
        self.assertEqual(plugins.env_keys, ("OPENVIKING_ENDPOINT", "OPENVIKING_USER", "OPENVIKING_ACCOUNT"))
        ov = plugins.config_files[".openviking/ov.conf"]["bot"]["ov_server"]
        self.assertEqual(ov["account_id"], "alice")
        self.assertEqual(ov["agent_id"], "asst-7")
        # A literal (the plugin's own compat token) survives verbatim.
        self.assertEqual(ov["root_api_key"], "astrabox-ip-trust")

    def test_plugin_config_deep_merges_into_defaults(self) -> None:
        plugins = h._build_hermes_plugins(_openviking_plugin_template(), runtime_vars=_RUNTIME_VARS)
        defaults = h._build_hermes_config_defaults(plugin_config_defaults=plugins.config_defaults)
        self.assertEqual(defaults["memory"], {"provider": "openviking"})
        # Platform base preserved alongside the plugin contribution.
        self.assertEqual(defaults["terminal"], {"backend": "local"})
        self.assertEqual(defaults["approvals"], {"mode": "off", "cron_mode": "approve"})

    def test_no_plugins_is_a_clean_neutral_passthrough(self) -> None:
        for model_config in ({}, {"hermes": {}}, {"hermes": {"plugins": []}}):
            plugins = h._build_hermes_plugins(model_config, runtime_vars=_RUNTIME_VARS)
            self.assertEqual(plugins.env, {})
            self.assertEqual(plugins.config_files, {})
            self.assertEqual(plugins.config_defaults, {})
            defaults = h._build_hermes_config_defaults(plugin_config_defaults=plugins.config_defaults)
            # No plugin => no forced memory provider; base defaults intact.
            self.assertNotIn("memory", defaults)
            self.assertEqual(defaults["terminal"], {"backend": "local"})

    def test_multiple_plugins_aggregate(self) -> None:
        model_config = {
            "hermes": {
                "plugins": [
                    {"name": "mem", "env": {"MEM_URL": "https://m"}, "config": {"memory": {"provider": "mem"}}},
                    {"name": "tel", "env": {"TEL_KEY": "k-{user_id}"}, "config": {"telemetry": {"on": True}}},
                ]
            }
        }
        plugins = h._build_hermes_plugins(model_config, runtime_vars=_RUNTIME_VARS)
        self.assertEqual(plugins.env, {"MEM_URL": "https://m", "TEL_KEY": "k-owner-1"})
        self.assertEqual(set(plugins.env_keys), {"MEM_URL", "TEL_KEY"})
        self.assertEqual(plugins.config_defaults, {"memory": {"provider": "mem"}, "telemetry": {"on": True}})

    def test_malformed_plugin_fails_loud(self) -> None:
        from astrabox.common.utils.errors import APIError

        with self.assertRaises(APIError):
            h._build_hermes_plugins({"hermes": {"plugins": ["not-an-object"]}}, runtime_vars=_RUNTIME_VARS)


if __name__ == "__main__":
    unittest.main()
