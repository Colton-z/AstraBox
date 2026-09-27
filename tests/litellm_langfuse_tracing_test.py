"""The gateway's Langfuse export runs only with both keys, and a partial setting is refused.

Compose forwards the three Langfuse variables with empty defaults, so an empty
value is the unconfigured case every installation starts in. LiteLLM is not
installed in the unit environment: its callback base class and its
``langfuse_otel`` logger are stood in for, and the stand-in logger records how
it was constructed.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "containers/litellm/langfuse_tracing.py"
_STUBBED = (
    "litellm",
    "litellm.integrations",
    "litellm.integrations.custom_logger",
    "litellm.integrations.langfuse",
    "litellm.integrations.langfuse.langfuse_otel",
)


class CustomLogger:  # the vendor base; its hooks do nothing unless overridden
    pass


class LangfuseOtelLogger(CustomLogger):
    def __init__(self, config=None, callback_name=None):
        self.config = config
        self.callback_name = callback_name


@pytest.fixture
def load(monkeypatch):
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(name, raising=False)
    saved = {name: sys.modules.get(name) for name in _STUBBED}
    for name in _STUBBED:
        sys.modules[name] = types.ModuleType(name)
    sys.modules["litellm.integrations.custom_logger"].CustomLogger = CustomLogger
    sys.modules["litellm.integrations.langfuse.langfuse_otel"].LangfuseOtelLogger = LangfuseOtelLogger

    def _load(**environment: str):
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        specification = importlib.util.spec_from_file_location("langfuse_tracing", MODULE)
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    try:
        yield _load
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_without_keys_the_gateway_registers_no_langfuse_logger(load) -> None:
    module = load()

    assert type(module.handler_instance) is CustomLogger


def test_compose_empty_defaults_leave_langfuse_off(load) -> None:
    module = load(LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="", LANGFUSE_HOST="")

    assert type(module.handler_instance) is CustomLogger


def test_both_keys_register_litellms_langfuse_otel_logger(load) -> None:
    module = load(
        LANGFUSE_PUBLIC_KEY="pk-lf-test",
        LANGFUSE_SECRET_KEY="sk-lf-test",
        LANGFUSE_HOST="https://langfuse.example.com",
    )

    handler = module.handler_instance
    assert isinstance(handler, LangfuseOtelLogger)
    # config=None makes LiteLLM build the exporter from the Langfuse variables.
    assert handler.config is None
    assert handler.callback_name == "langfuse_otel"


@pytest.mark.parametrize(
    ("present", "missing"),
    [("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"), ("LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY")],
)
def test_one_key_without_the_other_stops_the_gateway(load, present: str, missing: str) -> None:
    with pytest.raises(RuntimeError, match=f"{present} is set without {missing}"):
        load(**{present: "value"})


def test_a_host_without_keys_stops_the_gateway(load) -> None:
    with pytest.raises(RuntimeError, match="LANGFUSE_HOST is set without"):
        load(LANGFUSE_HOST="https://langfuse.example.com")
