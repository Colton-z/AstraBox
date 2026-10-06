"""The model catalog reaches the box, and Codex is told where it is.

Codex looks a model up by slug in `models.json`. Finding no entry it falls
back to its behaviour for an unknown model, which against this gateway
answered one message twice: two upstream requests, two `agentMessage` items,
and the reply duplicated in the transcript. The vendor's own integration guide
calls creating this file a required step, and the fields that decide the
transport (`use_responses_lite`, `multi_agent_version`) are in it.

The catalog is a box-create fact: the app-server reads it once, at boot.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.codex import (
    provider_config,
)


def _template(**options: Any) -> SimpleNamespace:
    return SimpleNamespace(engine_options=dict(options))


def test_the_thread_never_overrides_where_the_catalog_is() -> None:
    """The serving account's config.toml determines its catalog location.

    Both service shapes run the same catalog writer against their own
    CODEX_HOME. A `model_catalog_json` override in the thread config would
    name one account's path for every tenancy: the shared tenancy's server
    then answers thread/start with `failed to load configuration:
    Permission denied` against the box account's 0700 home.
    """

    config = provider_config("http://gw.test")
    assert "model_catalog_json" not in config
    assert config["web_search"] == "disabled"


def test_native_config_preserves_unknown_fields_but_rejects_owned_nodes() -> None:
    from astrabox.core.service.orchestrator.engine.codex import _engine_option, _model_catalog

    native = {"future_vendor_field": {"nested": [1, False]}, "approval_policy": "never"}
    assert _engine_option(_template(config=native), "config") == native
    assert _model_catalog(_template(model_catalog={})) == "{}"
    assert _model_catalog(_template()) is None
    with pytest.raises(ValueError, match="managed by AstraBox"):
        _engine_option(_template(config={"model_providers.astrabox.base_url": "other"}), "config")
    with pytest.raises(ValueError, match="managed by AstraBox"):
        _engine_option(
            _template(turn_start={"collaborationMode": {"settings": {"model": "other"}}}),
            "turn_start",
        )
