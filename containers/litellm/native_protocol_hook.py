"""Address native Messages under the DeepSeek Files namespace.

The alias invokes LiteLLM's unchanged Anthropic handler, authentication and
budget pipeline. Canonicalize the ASGI path before its dependencies classify
the request. A loopback passthrough would run the pipeline twice and bill twice.
"""

from __future__ import annotations

from fastapi import Depends, Request
from litellm.integrations.custom_logger import CustomLogger


async def canonical_messages_path(request: Request) -> None:
    request.scope["path"] = "/v1/messages"
    request.scope["raw_path"] = b"/v1/messages"


def register_native_messages_route() -> None:
    from litellm.proxy.anthropic_endpoints.endpoints import anthropic_response
    from litellm.proxy.proxy_server import app

    path = "/v1/deepseek/v1/messages"
    existing = [route for route in app.routes if getattr(route, "path", None) == path]
    if existing:
        if len(existing) == 1 and getattr(existing[0], "endpoint", None) is anthropic_response:
            return
        raise RuntimeError("the native DeepSeek Messages alias is already registered by another handler")
    app.add_api_route(
        path, anthropic_response, methods=["POST"],
        dependencies=[Depends(canonical_messages_path)],
    )


class AstraboxNativeProtocolHook(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        register_native_messages_route()


handler_instance = AstraboxNativeProtocolHook()
