"""Give streamed tool-call deltas the OpenAI shape every client accumulates.

OpenAI's streaming contract names a tool call once: the first delta for an
index carries ``id`` and ``function.name``, and later deltas for that index
carry only ``function.arguments``. Some upstreams send the later deltas with
those two fields present as empty strings. A client that merges deltas by
presence — the DeepSeek Harness's LLM package assigns ``callId`` whenever
the field is defined — then ends the call with an empty id and name, and
the turn fails on a tool call nobody can correlate.

The proxy serializes chunks with ``exclude_none``, so blanking the two
fields to ``None`` here removes them from the wire. Registered through
``litellm_settings.callbacks`` as ``stream_normalization_hook.handler_instance``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


def normalize_tool_call_deltas(chunk: Any) -> None:
    """Drop an empty ``id`` or ``function.name`` from a chunk's tool-call deltas."""

    for choice in getattr(chunk, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        for call in getattr(delta, "tool_calls", None) or []:
            if getattr(call, "id", None) == "":
                call.id = None
            function = getattr(call, "function", None)
            if function is not None and getattr(function, "name", None) == "":
                function.name = None


class AstraboxStreamNormalizationHook(CustomLogger):
    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: Any,
        request_data: dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        async for chunk in response:
            normalize_tool_call_deltas(chunk)
            yield chunk


handler_instance = AstraboxStreamNormalizationHook()
