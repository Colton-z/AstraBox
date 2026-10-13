"""Prepared resource synchronization over real repository CAS boundaries."""
from __future__ import annotations

import asyncio
import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.runtime import preparation as sync
from astrabox.persistence.repository import agent_repository
from astrabox.persistence.repository.sqlite.collection import AsyncCollection
from astrabox.persistence.repository.sqlite.engine import create_all, dispose_engines
from astrabox.seams import runtime_preparation as seam
from tests._postgresql_support import resolve_postgresql_test_url
from tests.open_sandbox_agent_pool_test import _Adapter, _PlatformProvider, _RuntimeManager, _settings, _template


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
async def repo(request, tmp_path, monkeypatch):
    url = resolve_postgresql_test_url() if request.param == "postgresql" else f"sqlite+aiosqlite:///{tmp_path}/resources.sqlite"
    await create_all(url)
    collection = AsyncCollection(f"resources_{uuid.uuid4().hex}", url)
    monkeypatch.setattr(agent_repository, "get_async_collection", AsyncMock(return_value=collection))
    template = _template(version=1, prewarm_revision="first")
    await collection.insert_one({
        "agent_id": template.agent_id, "user_id": "original-creator", "deleted": False,
        **sync._generation(vars(template)),
    })
    try:
        yield agent_repository.AgentRepository(), template
    finally:
        await collection.delete_many({})
        await dispose_engines(url)


class Provider(seam.RuntimePreparationProvider):
    name = "deployment-resources"

    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.finish.set()
        self.contexts = []
        self.writes = []
        self.failure = None
        self.recovered = False
        self.operations = []

    async def synchronize(self, *, context):
        self.contexts.append(context)
        self.started.set()
        await self.finish.wait()
        if self.failure:
            raise self.failure
        value = await context.sandbox.files.read_bytes("/declaration")
        if not self.writes or self.writes[-1] != value:
            self.writes.append(value)

    async def recover(self, *, operation):
        self.operations.append(operation)
        return self.recovered


@pytest.fixture
def provider(monkeypatch):
    value = Provider()
    monkeypatch.setattr(seam, "_PROVIDER", value)
    return value


def box(content=b"first"):
    return SimpleNamespace(files=SimpleNamespace(read_bytes=AsyncMock(return_value=content)))


async def synchronize(repo, template, sandbox=None):
    await sync.synchronize_prepared_resources(
        template, sandbox or box(), timeout_seconds=1, agent_repo=repo,
    )


async def advance(repo, template, **changes):
    updated = copy.deepcopy(template)
    vars(updated).update(changes)
    await repo.update_agent(updated.agent_id, sync._generation(vars(updated)))
    return updated


async def test_rewarm_reads_new_content_and_keeps_identity_context(repo, provider):
    store, template = repo
    template.packages = [{"kind": "claude_plugin", "url": "https://git.test/plugin.git", "protocol": "https", "paths": ["plugin"]}]
    template.skills = ["https://git.test/skills.git#example"]
    await synchronize(store, template)
    refreshed = await advance(store, template, client_pool_epoch="fresh", prewarm_revision="refresh")
    await synchronize(store, refreshed, box(b"changed"))
    await synchronize(store, refreshed, box(b"changed"))
    assert provider.writes == [b"first", b"changed"]
    assert {c.operation.agent_id for c in provider.contexts} == {template.agent_id}
    assert {c.creator_user_id for c in provider.contexts} == {"original-creator"}
    assert {c.config_version for c in provider.contexts} == {1}
    assert provider.contexts[-1].preparation_id == "fresh"
    assert provider.contexts[-1].packages[0].checkout_path.endswith("/00-plugin")
    assert provider.contexts[-1].packages[0].source["paths"] == ["plugin"]
    assert provider.contexts[-1].skills_path.startswith("/opt/conversation-runtime/claude-skills-cache/")
    assert provider.contexts[-1].config["model_config"] == template.model_config
    assert (await store.get_agent(template.agent_id))[sync._OPERATION] is None


async def test_obsolete_callback_cannot_follow_new_generation(repo, provider):
    store, template = repo
    provider.finish.clear()
    old = asyncio.create_task(synchronize(store, template, box(b"old")))
    await provider.started.wait()
    updated = await advance(store, template, version=2, client_pool_epoch="new")
    with pytest.raises(APIError, match="Another resource"):
        await synchronize(store, updated, box(b"new"))
    provider.finish.set()
    with pytest.raises(APIError) as stale:
        await old
    assert stale.value.code == "AGENT_RUNTIME_GENERATION_CONFLICT"
    await synchronize(store, updated, box(b"new"))
    with pytest.raises(APIError) as late:
        await synchronize(store, template, box(b"old"))
    assert late.value.code == "AGENT_RUNTIME_GENERATION_CONFLICT"
    assert provider.writes == [b"old", b"new"]


@pytest.mark.parametrize("failure", [RuntimeError("private remote secret"), asyncio.CancelledError()])
async def test_failure_preserves_uncertainty_until_provider_fences_it(repo, provider, failure):
    store, template = repo
    provider.failure = failure
    with pytest.raises((APIError, asyncio.CancelledError)) as failed:
        await synchronize(store, template)
    assert "private remote secret" not in str(failed.value)
    pending = (await store.get_agent(template.agent_id))[sync._OPERATION]
    assert pending["operation_id"] == provider.contexts[0].operation.operation_id
    provider.failure = None
    with pytest.raises(APIError) as blocked:
        await synchronize(store, template)
    assert blocked.value.code == "AGENT_RESOURCE_SYNC_UNRESOLVED"
    assert provider.writes == []
    provider.recovered = True
    await synchronize(store, template)
    assert provider.operations[-1].operation_id == pending["operation_id"]
    assert provider.writes == [b"first"]
    assert (await store.get_agent(template.agent_id))[sync._OPERATION] is None


async def test_expired_guard_is_not_permission_to_repeat_external_writes(repo, provider):
    store, template = repo
    await store.update_agent(template.agent_id, {sync._OPERATION: {
        "provider": provider.name, "operation_id": "lost-host", "deadline": 0,
    }})
    with pytest.raises(APIError) as blocked:
        await synchronize(store, template)
    assert blocked.value.code == "AGENT_RESOURCE_SYNC_UNRESOLVED"
    assert provider.contexts == []
    provider.recovered = True
    await synchronize(store, template)
    assert provider.operations[-1].operation_id == "lost-host"
    assert provider.writes == [b"first"]


async def test_changed_provider_cannot_recover_another_providers_operation(repo, provider):
    store, template = repo
    await store.update_agent(template.agent_id, {sync._OPERATION: {
        "provider": "other", "operation_id": "lost-host", "deadline": 0,
    }})
    provider.recovered = True
    with pytest.raises(APIError) as blocked:
        await synchronize(store, template)
    assert blocked.value.code == "AGENT_RESOURCE_SYNC_UNRESOLVED"
    assert provider.operations == []
    assert provider.contexts == []


async def test_timeout_does_not_release_uncertain_operation(repo, provider):
    store, template = repo
    provider.finish.clear()
    with pytest.raises(APIError) as failed:
        await sync.synchronize_prepared_resources(template, box(), timeout_seconds=0.01, agent_repo=store)
    assert failed.value.code == "AGENT_RESOURCE_SYNC_FAILED"
    assert (await store.get_agent(template.agent_id))[sync._OPERATION]["deadline"] == 0


@pytest.mark.parametrize("tenancy", ["agent", "conversation"])
@pytest.mark.parametrize("fail", [False, True])
async def test_pool_recipe_runs_extension_after_both_caches_before_runtime(repo, provider, monkeypatch, tenancy, fail):
    from astrabox.core.service.orchestrator.agent import prepared_slots, runtime_preparation
    store, template = repo
    template.sandbox_tenancy = tenancy
    events = []
    sandbox = box()

    async def plugins(*args, **kwargs):
        events.append("plugins")

    async def skills(*args, **kwargs):
        events.append("skills")

    async def read(path):
        assert set(events) == {"plugins", "skills"}
        events.append("sync")
        if fail:
            raise RuntimeError("resource read failed")
        return b"prepared"

    async def runtime(*args, **kwargs):
        assert provider.writes == [b"prepared"]
        events.append("runtime")

    sandbox.files.read_bytes = read
    monkeypatch.setattr(client_pool, "load_astrabox_settings", lambda: _settings())
    monkeypatch.setattr(client_pool, "sandbox_for_template", lambda _: _PlatformProvider())
    monkeypatch.setattr(client_pool, "get_engine_adapter", lambda _: _Adapter())
    monkeypatch.setattr(client_pool, "get_underlying_sandbox", lambda _: sandbox)
    monkeypatch.setattr(client_pool, "extract_sandbox_id", lambda _: "box")
    monkeypatch.setattr(client_pool, "resolve_runtime_profile", lambda *args, **kwargs: object())
    monkeypatch.setattr(client_pool, "probe_sandbox_runtime_profile", AsyncMock())
    monkeypatch.setattr(client_pool, "prepare_agent_runtime_plugin_cache", plugins)
    monkeypatch.setattr(client_pool, "prepare_agent_runtime_skill_cache", skills)
    monkeypatch.setattr(prepared_slots, "prepare_pool_seats", runtime)
    monkeypatch.setattr(runtime_preparation, "prepare_pool_runtime", runtime)
    plan = await client_pool.build_agent_client_pool_plan(template, runtime_manager=_RuntimeManager(), agent_repo=store)
    assert plan is not None
    if fail:
        with pytest.raises(APIError) as failed:
            await plan.preparer(sandbox)
        assert failed.value.code == "AGENT_RESOURCE_SYNC_FAILED"
        assert "runtime" not in events
    else:
        await plan.preparer(sandbox)
        assert events[-2:] == ["sync", "runtime"]


async def test_no_provider_preserves_preparation_without_repository(monkeypatch):
    monkeypatch.setattr(seam, "_PROVIDER", None)
    await sync.synchronize_prepared_resources(object(), box(), timeout_seconds=1)
    await sync.assert_prepared_resources_current(object())



async def test_two_replicas_racing_to_claim_one_agent_do_not_duplicate_the_identity(repo, provider):
    store, template = repo
    provider.finish.clear()
    tasks = [asyncio.create_task(synchronize(agent_repository.AgentRepository(), template)) for _ in range(4)]
    try:
        await provider.started.wait()
        # Let every contender reach the durable CAS while the winner is held.
        await asyncio.wait_for(asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED), timeout=5)
        provider.finish.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert any(result is None for result in results)
        assert all(result is None or isinstance(result, APIError) and result.code == "AGENT_RESOURCE_SYNC_BUSY" for result in results)
        assert provider.writes == [b"first"]
        assert (await store.get_agent(template.agent_id))[sync._OPERATION] is None
    finally:
        provider.finish.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_provider_cannot_mutate_the_snapshot_used_by_generation_checks(repo, provider):
    store, template = repo

    async def mutate(*, context):
        context.config["version"] = 2
        await store.update_agent(template.agent_id, {"version": 2})

    provider.synchronize = mutate
    with pytest.raises(APIError) as stale:
        await synchronize(store, template)
    assert stale.value.code == "AGENT_RUNTIME_GENERATION_CONFLICT"
    assert template.version == 1


async def test_callback_reads_changed_branch_from_actual_prepared_checkout(repo, provider, tmp_path, monkeypatch):
    import os
    import subprocess
    from pathlib import Path

    from astrabox.core.service.orchestrator.runtime.storage import _git_clone, _plugin_cache
    from astrabox.seams.sandbox_data import SandboxCommandResult

    store, template = repo
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    manifest = source / ".astrabox" / "resources.json"
    manifest.parent.mkdir()
    source_url = "https://fixture.test/resources.git"
    environment = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.file://{source}.insteadOf",
        "GIT_CONFIG_VALUE_0": source_url,
    }

    class Commands:
        async def run(self, command, *, timeout=None):
            process = await asyncio.create_subprocess_exec(
                "bash", "-c", command, env=environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout or 30)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            return SandboxCommandResult(exit_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())

    async def read_bytes(path):
        return Path(path).read_bytes()

    async def synchronize_manifest(*, context):
        content = await context.sandbox.files.read_bytes(context.packages[0].checkout_path + "/.astrabox/resources.json")
        provider.contexts.append(context)
        if not provider.writes or provider.writes[-1] != content:
            provider.writes.append(content)

    sandbox = SimpleNamespace(commands=Commands(), files=SimpleNamespace(read_bytes=read_bytes))
    provider.synchronize = synchronize_manifest
    template.packages = [{"kind": "claude_plugin", "url": source_url, "protocol": "https", "branch": "main"}]
    monkeypatch.setattr(_git_clone, "_CLONE_STAGE_ROOT", str(tmp_path / "staging"))
    for number, content in enumerate((b'{"resource":"first"}', b'{"resource":"changed"}', b'{"resource":"changed"}')):
        manifest.write_bytes(content)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                        "commit", "--allow-empty", "-m", "Resource declaration"], check=True, capture_output=True)
        cache = str(tmp_path / f"member-{number}")
        monkeypatch.setattr(_plugin_cache, "AGENT_RUNTIME_PACKAGE_CACHE_DIR", cache)
        monkeypatch.setattr(_plugin_cache, "AGENT_RUNTIME_PACKAGE_CACHE_BASE_DIR", cache + "/repos")
        template = await advance(store, template, client_pool_epoch=f"refresh-{number}", prewarm_revision=str(number))
        await _plugin_cache.prepare_agent_runtime_plugin_cache(sandbox, template, get_underlying_sandbox_fn=lambda handle: handle)
        await synchronize(store, template, sandbox)
    assert provider.writes == [b'{"resource":"first"}', b'{"resource":"changed"}']
    assert {context.operation.agent_id for context in provider.contexts} == {template.agent_id}
    assert {context.config_version for context in provider.contexts} == {1}
