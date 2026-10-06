"""Sampling charges viewer identity and shares admission across service replicas."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.artifacts import sample
from astrabox.core.service.artifacts.sample import ArtifactSampleService
from astrabox.seams.model import ModelEndpoint
from tests.artifact_service_test import OWNER, create_page
from tests.artifact_service_test import artifact_service as artifact_service

BODY = {"input": [{"role": "user", "content": "hello"}]}


def replicas(service, monkeypatch, requests):
    endpoint = ModelEndpoint(base_url="https://model.example/v1", api_key="server-key")
    def headers(*, endpoint, context):
        assert context.user_id == OWNER.user_id
        return {"x-viewer": context.user_id, "x-conversation": context.conversation_id}
    provider = SimpleNamespace(server_endpoint=lambda **kw: endpoint, request_headers=headers)
    monkeypatch.setattr(sample, "model_endpoint_for_name", lambda _: provider)
    monkeypatch.setattr(sample, "load_astrabox_settings", lambda: None)
    monkeypatch.setattr(sample, "RuntimeConfigResolver", lambda _: SimpleNamespace(
        resolve_endpoint_provider=lambda _: "test", resolve_model_config=lambda _: {"model_name": "route"}))
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    origin = SimpleNamespace(authorize=AsyncMock(return_value=SimpleNamespace(model_config={})))
    return [ArtifactSampleService(artifacts=service, origin_access=origin,
        http_client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw)) for _ in range(2)]


@pytest.mark.asyncio
async def test_sample_concurrency_is_shared_across_replicas_and_artifacts(artifact_service, monkeypatch) -> None:
    requests = []
    first, second = replicas(artifact_service, monkeypatch, requests)
    viewers = [await artifact_service.resolve_viewer(user=OWNER, artifact_id=await create_page(artifact_service, {"sample": {}})) for _ in range(2)]
    streams = [await first.prepare(viewers[0], BODY), await second.prepare(viewers[1], BODY)]
    try:
        with pytest.raises(APIError) as error:
            await second.prepare(viewers[0], BODY)
        assert error.value.code == "ARTIFACT_SAMPLE_RATE_LIMITED"
    finally:
        for stream in streams:
            await stream.aclose()
    stream = await second.prepare(viewers[0], BODY)
    await stream.aclose()
    await stream.aclose()
    assert requests == []


@pytest.mark.asyncio
async def test_sample_per_minute_budget_survives_release_and_replica_change(artifact_service, monkeypatch) -> None:
    first, second = replicas(artifact_service, monkeypatch, [])
    viewer = await artifact_service.resolve_viewer(user=OWNER, artifact_id=await create_page(artifact_service, {"sample": {}}))
    for _ in range(30):
        await (await first.prepare(viewer, BODY)).aclose()
    with pytest.raises(APIError) as error:
        await second.prepare(viewer, BODY)
    assert error.value.code == "ARTIFACT_SAMPLE_RATE_LIMITED"


@pytest.mark.asyncio
async def test_sample_gateway_headers_attribute_viewer_with_server_credential(artifact_service, monkeypatch) -> None:
    requests = []
    first, _ = replicas(artifact_service, monkeypatch, requests)
    aid = await create_page(artifact_service, {"sample": {}})
    viewer = await artifact_service.resolve_viewer(user=OWNER, artifact_id=aid)
    stream = await first.prepare(viewer, BODY)
    events = [event async for event in stream]
    await stream.aclose()
    assert any('"answer"' in event for event in events)
    assert requests[0].headers["x-viewer"] == "owner"
    assert requests[0].headers["x-conversation"] == "artifact:" + aid
    assert requests[0].headers["authorization"] == "Bearer server-key"


@pytest.mark.asyncio
async def test_sample_route_limits_raw_json_bytes_before_spending(monkeypatch) -> None:
    from fastapi import FastAPI
    from astrabox.api.routes import artifacts
    from astrabox.core.service.artifacts.service import ArtifactViewer
    from astrabox.core.service.artifacts import limits
    app = FastAPI()
    artifacts.register_artifact_routes(app)
    viewer = ArtifactViewer("art", OWNER, "owner", "id", False)
    app.dependency_overrides[artifacts.owner_viewer] = lambda: viewer
    samples = SimpleNamespace(prepare=AsyncMock())
    monkeypatch.setattr(artifacts, "get_platform_service", lambda: SimpleNamespace(artifact_samples=samples))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/artifacts/art/runtime/sample", content=b'{"input":[{"role":"user","content":"hello"}]}' + b' ' * limits.SAMPLE_REQUEST_BYTES, headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json()["code"] == "ARTIFACT_SAMPLE_TOO_LARGE"
    samples.prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_sample_cannot_start_after_its_admission_deadline(artifact_service, monkeypatch) -> None:
    requests = []
    first, second = replicas(artifact_service, monkeypatch, requests)
    viewer = await artifact_service.resolve_viewer(user=OWNER, artifact_id=await create_page(artifact_service, {"sample": {}}))
    monkeypatch.setattr(sample.limits, "SAMPLE_REQUEST_TIMEOUT_SECONDS", 0)
    stream = await first.prepare(viewer, BODY)
    events = [event async for event in stream]
    assert any('event: error' in event for event in events)
    assert requests == []
    monkeypatch.setattr(sample.limits, "SAMPLE_REQUEST_TIMEOUT_SECONDS", 60)
    await (await second.prepare(viewer, BODY)).aclose()


@pytest.mark.asyncio
async def test_expired_crashed_sample_reservations_do_not_hold_concurrency(artifact_service, monkeypatch) -> None:
    first, second = replicas(artifact_service, monkeypatch, [])
    viewer = await artifact_service.resolve_viewer(user=OWNER, artifact_id=await create_page(artifact_service, {"sample": {}}))
    streams = [await first.prepare(viewer, BODY) for _ in range(2)]
    rows = await artifact_service.viewers.list({})
    budget = next(row for row in rows if row["doc_id"].startswith("sample_budget:"))
    for entry in budget["samples"]:
        entry["created_at"] = "2000-01-01T00:00:00+00:00"
        entry["expires_at"] = "2000-01-01T00:00:00+00:00"
    await artifact_service.viewers.put(budget)
    await (await second.prepare(viewer, BODY)).aclose()
    for stream in streams:
        await stream.aclose()
