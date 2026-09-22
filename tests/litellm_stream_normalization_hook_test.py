"""The gateway hook restores the OpenAI streaming shape for tool-call deltas.

The chunks are what an OpenAI-compatible relay streams for
``deepseek-v4-flash``: the first delta names the call, every later delta
repeats ``id`` and ``name`` as empty strings. LiteLLM is not installed in the unit environment, so its
base class is stood in for and the chunks are attribute objects shaped like
its ``ModelResponseStream``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[1] / "containers/litellm/stream_normalization_hook.py"


@pytest.fixture
def hook_module():
    stub = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:  # the vendor base; only inheritance matters here
        pass

    stub.CustomLogger = CustomLogger
    saved = {name: sys.modules.get(name) for name in ("litellm", "litellm.integrations", "litellm.integrations.custom_logger")}
    sys.modules["litellm"] = types.ModuleType("litellm")
    sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
    sys.modules["litellm.integrations.custom_logger"] = stub
    try:
        specification = importlib.util.spec_from_file_location("stream_normalization_hook", MODULE)
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _chunk(*, call_id: str, name: str, arguments: str) -> SimpleNamespace:
    function = SimpleNamespace(name=name, arguments=arguments)
    call = SimpleNamespace(id=call_id, type="function", index=0, function=function)
    return SimpleNamespace(choices=[SimpleNamespace(index=0, delta=SimpleNamespace(content=None, tool_calls=[call]))])


def test_the_first_delta_keeps_the_calls_identity(hook_module) -> None:
    chunk = _chunk(call_id="call_0c1c0f53fac2497985b713d8", name="bash", arguments="")

    hook_module.normalize_tool_call_deltas(chunk)

    call = chunk.choices[0].delta.tool_calls[0]
    assert call.id == "call_0c1c0f53fac2497985b713d8"
    assert call.function.name == "bash"


def test_a_later_delta_loses_its_empty_id_and_name(hook_module) -> None:
    chunk = _chunk(call_id="", name="", arguments='"command": "echo')

    hook_module.normalize_tool_call_deltas(chunk)

    call = chunk.choices[0].delta.tool_calls[0]
    # None is what the proxy's exclude_none serialization drops from the wire.
    assert call.id is None
    assert call.function.name is None
    assert call.function.arguments == '"command": "echo'


def test_chunks_without_tool_calls_pass_untouched(hook_module) -> None:
    chunk = SimpleNamespace(choices=[SimpleNamespace(index=0, delta=SimpleNamespace(content="hi", tool_calls=None))])

    hook_module.normalize_tool_call_deltas(chunk)

    assert chunk.choices[0].delta.content == "hi"


@pytest.mark.asyncio
async def test_the_hook_yields_every_chunk_normalized(hook_module) -> None:
    async def upstream():
        yield _chunk(call_id="call_1", name="bash", arguments="")
        yield _chunk(call_id="", name="", arguments="{}")

    hook = hook_module.handler_instance
    seen = [c async for c in hook.async_post_call_streaming_iterator_hook(None, upstream(), {})]

    assert [c.choices[0].delta.tool_calls[0].id for c in seen] == ["call_1", None]
