"""Send the gateway's model calls to Langfuse when both Langfuse keys are set.

LiteLLM integrates Langfuse v3 and v4 through its ``langfuse_otel`` logger,
which exports every model call as an OpenTelemetry span to
``<host>/api/public/otel``
(https://docs.litellm.ai/docs/observability/langfuse_integration,
https://langfuse.com/integrations/gateways/litellm). LiteLLM's ``langfuse``
callback is the integration for the Langfuse Python SDK v2 instead: LiteLLM
declares ``langfuse<3`` for it, and Langfuse v4 rejects that SDK's batch
ingestion (https://langfuse.com/docs/compatibility).

Naming ``langfuse_otel`` in the configuration would not keep the integration
off without keys. LiteLLM then builds the logger from the generic ``OTEL_*``
environment, which prints every span, prompts included, to standard output,
or sends it to whatever ``OTEL_EXPORTER_OTLP_ENDPOINT`` the gateway process
inherits. This module therefore constructs LiteLLM's logger only when
``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are both set, and
otherwise registers a callback with no hooks. One key without the other, or
``LANGFUSE_HOST`` without the keys, stops the gateway at startup: that
configuration would send nothing and report nothing.

LiteLLM reads the destination itself: ``LANGFUSE_OTEL_HOST``, then
``LANGFUSE_HOST``, then Langfuse Cloud US (``https://us.cloud.langfuse.com``).

Registered through ``litellm_settings.callbacks`` as
``langfuse_tracing.handler_instance``.
"""

from __future__ import annotations

import os

from litellm.integrations.custom_logger import CustomLogger

_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def _set(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def langfuse_enabled() -> bool:
    """Whether the Langfuse keys configure an export, refusing a partial setting."""

    keys = [name for name in _KEYS if _set(name)]
    if len(keys) == len(_KEYS):
        return True
    if keys:
        missing = next(name for name in _KEYS if name not in keys)
        raise RuntimeError(
            f"{keys[0]} is set without {missing}; Langfuse needs both keys to "
            "accept the gateway's traces. Set both, or neither to leave "
            "Langfuse off."
        )
    if _set("LANGFUSE_HOST"):
        raise RuntimeError(
            "LANGFUSE_HOST is set without LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY, so no trace would be sent. Set both keys, "
            "or unset LANGFUSE_HOST to leave Langfuse off."
        )
    return False


def _handler() -> CustomLogger:
    if not langfuse_enabled():
        return CustomLogger()
    from litellm.integrations.langfuse.langfuse_otel import LangfuseOtelLogger

    # The same construction LiteLLM performs for the `langfuse_otel` callback.
    return LangfuseOtelLogger(config=None, callback_name="langfuse_otel")


handler_instance = _handler()
