"""One streamed OpenAI-compatible model round, with no platform tool execution."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncGenerator, AsyncIterator, Callable

import httpx

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.runtime.config_resolver import RuntimeConfigResolver
from astrabox.seams.model import ModelRequestContext, model_endpoint_for_name
from astrabox.common.utils.time_utils import parse_iso, utcnow, utcnow_iso, plus_seconds_iso
from astrabox.persistence.repository.backend import DuplicateKeyError

from . import limits
from .origin import OriginAccess
from .service import ArtifactService, ArtifactViewer, invalid, opaque_viewer_id, sse

logger = get_logger(__name__)
SYSTEM_MESSAGE = (
    "You are answering a request from an interactive page. You have only the tools "
    "provided in this request. Follow the format requested by the user."
)


def validate_sample_request(body: dict[str, Any]) -> dict[str, Any]:
    if ArtifactService._json_size(body) > limits.SAMPLE_REQUEST_BYTES:
        raise APIError(code="ARTIFACT_SAMPLE_TOO_LARGE", message="sample request exceeds 256 KiB", status_code=413)
    messages = body.get("messages") if body.get("messages") is not None else body.get("input")
    if not isinstance(messages, list) or not messages:
        raise invalid("sample requires messages")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) - {"role", "content", "tool_calls", "tool_call_id"}
            or message.get("role") not in {"user", "assistant", "tool"}
            or not isinstance(message.get("content"), (str, type(None)))
        ):
            raise invalid("messages accept user, assistant and tool roles with text content")
        if message.get("content") is None and not message.get("tool_calls"):
            raise invalid("null content requires tool_calls")
        if message.get("tool_calls") is not None and (
            message["role"] != "assistant" or not isinstance(message["tool_calls"], list)
        ):
            raise invalid("tool_calls belong to assistant messages")
        if message["role"] == "tool" and not isinstance(message.get("tool_call_id"), str):
            raise invalid("tool messages require tool_call_id")
    max_tokens = body.get("max_tokens", limits.SAMPLE_DEFAULT_MAX_TOKENS)
    if type(max_tokens) is not int or max_tokens < 1:
        raise invalid("max_tokens must be positive")
    if max_tokens > limits.SAMPLE_MAX_TOKENS:
        raise APIError(code="ARTIFACT_SAMPLE_TOKEN_LIMIT", message="sample max_tokens exceeds 8192", status_code=400)
    result: dict[str, Any] = {
        "messages": [{"role": "system", "content": SYSTEM_MESSAGE}, *messages],
        "stream": True,
        "max_tokens": max_tokens,
    }
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        raise invalid("tools must be an array")
    if tools:
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or set(tool) != {"name", "description", "input_schema"}
                or not isinstance(tool["name"], str)
                or not tool["name"]
                or not isinstance(tool["description"], str)
                or not isinstance(tool["input_schema"], dict)
            ):
                raise invalid("tools require name, description and input_schema")
        result["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
    ArtifactService._json_size(result)
    return result


class SampleStream:
    """Release an admission even when a viewer disconnects before iteration."""

    def __init__(self, stream: AsyncGenerator[str, None], release: Callable[..., Any]) -> None:
        self._stream, self._release = stream, release

    def __aiter__(self) -> AsyncIterator[str]:
        return self._stream

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            await self._release()


class ArtifactSampleService:
    def __init__(
        self,
        *,
        artifacts: ArtifactService,
        origin_access: OriginAccess,
        http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    ) -> None:
        self._artifacts, self._origin_access = artifacts, origin_access
        self._http_client_factory = http_client_factory

    @staticmethod
    def _limited() -> APIError:
        return APIError(code="ARTIFACT_SAMPLE_RATE_LIMITED", message="sample request limit reached", status_code=429)

    async def _reserve(self, user_id: str, reservation: str) -> str:
        key = "sample_budget:" + opaque_viewer_id("sample-budget", user_id)

        async def write(tx: Any) -> None:
            budget = await self._artifacts.viewers.lock(key, transaction=tx)
            now = utcnow()
            entries = [e for e in (budget or {}).get("samples", []) if
                (now - parse_iso(e["created_at"])).total_seconds() < limits.SAMPLE_WINDOW_SECONDS
                or (e["active"] and now < parse_iso(e["expires_at"]))]
            active = sum(e["active"] and now < parse_iso(e["expires_at"]) for e in entries)
            recent = sum((now - parse_iso(e["created_at"])).total_seconds() < limits.SAMPLE_WINDOW_SECONDS for e in entries)
            if active >= limits.SAMPLE_CONCURRENT or recent >= limits.SAMPLE_PER_MINUTE:
                raise self._limited()
            entries.append({"reservation": reservation, "created_at": utcnow_iso(), "active": True,
                "expires_at": plus_seconds_iso(limits.SAMPLE_REQUEST_TIMEOUT_SECONDS + limits.LIVE_HEARTBEAT_SECONDS)})
            row = {"doc_id": key, "samples": entries}
            if budget:
                await self._artifacts.viewers.put(row, transaction=tx)
            else:
                await self._artifacts.viewers.insert(row, transaction=tx)

        try:
            await self._artifacts.transaction(write)
        except DuplicateKeyError as exc:
            raise self._limited() from exc
        return key

    async def _release(self, key: str, reservation: str) -> None:
        async def write(tx: Any) -> None:
            budget = await self._artifacts.viewers.lock(key, transaction=tx)
            if budget is None:
                raise APIError(code="ARTIFACT_STORE_UNAVAILABLE", message="sample reservation budget is missing", status_code=503)
            for entry in budget["samples"]:
                if entry["reservation"] == reservation:
                    entry["active"] = False
            await self._artifacts.viewers.put(budget, transaction=tx)
        await self._artifacts.transaction(write)

    async def prepare(self, viewer: ArtifactViewer, body: dict[str, Any]) -> SampleStream:
        request = validate_sample_request(body)
        viewer, record, version = await self._artifacts.refresh(viewer)
        self._artifacts.require(viewer, version, "sample")
        harness = await self._origin_access.authorize(viewer, record)
        settings = load_astrabox_settings()
        try:
            resolver = RuntimeConfigResolver(settings)
            config = dict(harness.model_config or {})
            provider = model_endpoint_for_name(resolver.resolve_endpoint_provider(config))
            endpoint = provider.server_endpoint(settings=settings)
            route = resolver.resolve_model_config(config).get("model_name")
            if not endpoint.base_url or not endpoint.api_key or not route:
                raise ValueError("origin model has no server endpoint or route")
        except Exception as exc:
            logger.warning("artifact sample endpoint unavailable: %s", exc)
            raise APIError(
                code="ARTIFACT_SAMPLE_UNAVAILABLE",
                message="origin model endpoint is unavailable",
                status_code=503,
            ) from exc
        request["model"] = route
        if viewer.user is None:
            raise APIError(
                code="ARTIFACT_SAMPLE_DENIED", message="sample requires sign-in", status_code=403
            )
        headers = dict(provider.request_headers(endpoint=endpoint, context=ModelRequestContext(
            user_id=viewer.user.user_id, conversation_id=f"artifact:{viewer.artifact_id}"
        )))
        headers.update({"authorization": f"Bearer {endpoint.api_key}", "content-type": "application/json"})
        reservation = uuid.uuid4().hex
        deadline = asyncio.get_running_loop().time() + limits.SAMPLE_REQUEST_TIMEOUT_SECONDS
        key = await self._reserve(viewer.user.user_id, reservation)

        async def release() -> None:
            await self._release(key, reservation)

        return SampleStream(self._stream(endpoint, request, headers, deadline, release), release)

    async def _stream(
        self, endpoint: Any, request: dict[str, Any], headers: dict[str, str], deadline: float, release: Callable[..., Any]
    ) -> AsyncGenerator[str, None]:
        text, calls, finished, truncated = "", {}, False, False
        try:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("sample admission expired before streaming")
            async with asyncio.timeout_at(deadline), self._http_client_factory(
                timeout=limits.SAMPLE_REQUEST_TIMEOUT_SECONDS
            ) as client:
                async with client.stream(
                    "POST",
                    f"{endpoint.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=request,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            finished = True
                            break
                        payload = json.loads(raw)
                        if not isinstance(payload, dict) or payload.get("error"):
                            raise ValueError("model returned an error or invalid stream object")
                        for choice in payload.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            delta = choice.get("delta", {})
                            fragment = delta.get("content") or ""
                            if fragment:
                                text += fragment
                                yield sse("delta", {"text": fragment})
                            for chunk in delta.get("tool_calls", []):
                                index = chunk["index"]
                                call = calls.setdefault(
                                    index,
                                    {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                if chunk.get("id"):
                                    call["id"] = chunk["id"]
                                for field in ("name", "arguments"):
                                    call["function"][field] += chunk.get("function", {}).get(
                                        field, ""
                                    )
                            if choice.get("finish_reason"):
                                finished = True
                                truncated = choice["finish_reason"] == "length"
            if not finished:
                raise ValueError("model stream ended without a terminal marker")
            yield sse(
                "done",
                {
                    "text": text,
                    "tool_calls": [calls[i] for i in sorted(calls)],
                    "truncated": truncated,
                },
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            logger.warning("artifact sample stream failed: %s", exc)
            yield sse(
                "error", {"code": "unavailable", "message": "model request failed", "text": text}
            )
        finally:
            await release()
