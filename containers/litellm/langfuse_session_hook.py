"""Copy AstraBox Session identity from the presented key into trace metadata.

A prepared engine child's environment is frozen before its platform Session
exists, so it cannot send ``langfuse_session_id`` headers the way host-built
children do. Instead, the claim path mints a Session-scoped virtual key whose
metadata carries the identity, and this hook copies it into the request
metadata fields LiteLLM's Langfuse callback actually reads. Nothing native
does this: key metadata is stored on every request but the Langfuse logger
only pops ``session_id`` and ``trace_*`` from request metadata.

Route correctness matters and fails silently when wrong: ``/v1/messages``-
family routes carry their metadata under ``litellm_metadata`` while
``/chat/completions`` uses ``metadata``. Rather than re-deriving LiteLLM's
route table, the hook writes into whichever variable the proxy already
populated for this request — the proxy placed ``user_api_key_metadata`` there
before any hook runs, so exactly one of the two is present.

Registered via ``litellm_settings.callbacks`` as
``langfuse_session_hook.handler_instance``, mounted beside ``custom_auth.py``.
"""

from __future__ import annotations

from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_SESSION_FIELD = "astrabox_session_id"
_USER_FIELD = "astrabox_user_id"


class AstraboxLangfuseSessionHook(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any] | None:
        key_metadata = getattr(user_api_key_dict, "metadata", None)
        if key_metadata is None:
            return data
        if not isinstance(key_metadata, dict):
            # A Session key always carries a dict; anything else is a
            # corrupted key row and must not pass silently.
            raise ValueError(
                "presented key carries non-mapping metadata; refusing to "
                "attribute this request"
            )
        session_id = str(key_metadata.get(_SESSION_FIELD) or "").strip()
        if not session_id:
            # Keys that legitimately carry no Session (the shared sandbox
            # key, operator keys) pass through untouched.
            return data
        user_id = str(key_metadata.get(_USER_FIELD) or "").strip()
        target = None
        for variable in ("litellm_metadata", "metadata"):
            if isinstance(data.get(variable), dict):
                target = data[variable]
                break
        if target is None:
            target = data.setdefault("metadata", {})
        # A client that explicitly sent its own attribution wins; the key is
        # the fallback identity for children that cannot send headers.
        target.setdefault("session_id", session_id)
        if user_id:
            target.setdefault("trace_user_id", user_id)
        return data


handler_instance = AstraboxLangfuseSessionHook()
