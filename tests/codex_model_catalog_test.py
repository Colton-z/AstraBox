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

import json
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.codex import (
    CODEX_MODEL_CATALOG_ENV_VAR,
    provider_config,
)

_CATALOG = json.dumps({"models": [{"slug": "deepseek-v4-flash"}]})
_CONTRACT_CATALOG = (
    __import__("pathlib")
    .Path(__file__)
    .resolve()
    .parents[1]
    .joinpath("tests/e2e-contract/codex-model-catalog.json")
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


def test_the_catalog_env_var_is_the_one_the_image_reads() -> None:
    """Named here and in the image's one catalog writer, across the box seam.

    Both service shapes (the box-level server and a conversation's own
    instance) run the same helper, so this pins the helper rather than a
    script that merely calls it.
    """

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    writer = root.joinpath("containers/sandbox-codex/astrabox-model-catalog.py").read_text()
    assert f'"{CODEX_MODEL_CATALOG_ENV_VAR}"' in writer
    assert 'codex_home / "models.json"' in writer
    for script in (
        "containers/sandbox-codex/astrabox-codex-serve",
        "containers/sandbox-codex/astrabox-codex-serve-conversation",
    ):
        assert "model-catalog.py" in root.joinpath(script).read_text(), script


def test_the_engine_option_declares_the_catalog() -> None:
    from astrabox.core.service.orchestrator.engine.codex import (
        CODEX_ENGINE_OPTIONS_SCHEMA,
    )

    keys = {option["key"] for option in CODEX_ENGINE_OPTIONS_SCHEMA}
    assert keys == {"model_catalog", "config", "turn_start"}
    assert _template(model_catalog=json.loads(_CATALOG)).engine_options["model_catalog"]
    assert all(option["type"] == "object" for option in CODEX_ENGINE_OPTIONS_SCHEMA)


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


def test_the_deepseek_catalog_preserves_the_selected_native_transport() -> None:
    catalog = json.loads(_CONTRACT_CATALOG.read_text(encoding="utf-8"))
    models = catalog["models"]
    assert {model["slug"] for model in models} == {
        "deepseek-flash",
        "deepseek-v4-pro",
    }
    assert all(model["apply_patch_tool_type"] is None for model in models)
    assert all(model["web_search_tool_type"] == "text_and_image" for model in models)
    assert all(model["tool_mode"] == "direct" for model in models)
    assert all(model["supports_search_tool"] is False for model in models)
    assert all(model["multi_agent_version"] == "v1" for model in models)
    assert all(model["use_responses_lite"] is False for model in models)
