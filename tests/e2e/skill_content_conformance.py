"""Bind the Skills contract to a live OpenSandbox box and installed suppliers."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path


async def run(box_id: str, kind: str, declaration: dict, fixture: dict) -> None:
    from astrabox.deploy.onebox import _export_backend_wiring, ensure_database_wiring, needs_sandbox_server

    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    from astrabox.bootstrap import bootstrap

    bootstrap()
    from astrabox.core.service.orchestrator.runtime.skill_sources import prepare_agent_runtime_skill_cache, skill_cache_dir
    from astrabox.seams.sandbox import sandbox_for_name
    from astrabox.seams.skills import skill_content_provider
    from astrabox.testing.skill_content_conformance import (
        check_cancelled_acquisition, check_installed_skill,
        check_no_skill_credentials, check_refused_sources,
    )
    from astrabox_test_skills import ROOT

    sandbox = await sandbox_for_name("open_sandbox").connect(box_id)
    provider = skill_content_provider(declaration["provider"])
    original_fetch = provider.fetch
    fetched = []

    async def observed_fetch(*args, **kwargs):
        fetched.append(args[-1])
        return await original_fetch(*args, **kwargs)

    provider.fetch = observed_fetch
    try:
        expected = (ROOT / "version-1.md").read_bytes()
        receipt = await check_installed_skill(sandbox, declaration, name=declaration["name"], expected=expected)
        assert receipt["version"] == declaration["version"]
        await prepare_agent_runtime_skill_cache(sandbox, [declaration])
        assert not fetched, "a cache hit fetched the source again"
        await check_no_skill_credentials(sandbox)

        if kind == "ssh":
            failures = []
            for key, code in (("not-authorized", "AGENT_DEPLOY_KEY_NOT_ALLOWED"),
                              ("fixture-skill-missing", "AGENT_RUNTIME_SKILL_CACHE_FAILED"),
                              ("fixture-skill-wrong", "AGENT_RUNTIME_SKILL_CACHE_FAILED")):
                bad = copy.deepcopy(declaration)
                bad["config"]["deploy_key_secret_name"] = key
                failures.append((bad, code))
            bad_host = copy.deepcopy(declaration)
            bad_host["config"]["known_hosts"] = fixture["wrong_known_hosts"]
            failures.append((bad_host, "AGENT_RUNTIME_SKILL_CACHE_FAILED"))
            bad_repo = copy.deepcopy(declaration)
            bad_repo["source"] = declaration["source"].replace("/skills.git", "/denied.git")
            failures.append((bad_repo, "AGENT_RUNTIME_SKILL_CACHE_FAILED"))
            await check_refused_sources(sandbox, failures)
            variants = [{**declaration, "name": f"parallel-{index}"} for index in (1, 2)]
            fetched.clear()
            await asyncio.gather(*(prepare_agent_runtime_skill_cache(sandbox, [source]) for source in variants))
            assert len(fetched) == 2 and len(set(fetched)) == 2, "concurrent fetches shared credential directories"
            for source in variants:
                await check_installed_skill(sandbox, source, name=source["name"], expected=expected)
            slow = {**declaration, "source": declaration["source"].replace("/skills.git", "/slow.git")}

            async def started() -> bool:
                return (ROOT / "slow-fetch-started").exists()

            await check_cancelled_acquisition(sandbox, slow, started=started)
            # The fixture's held native Git process exits after four seconds.
            # Its late completion still must not publish or restore credentials.
            await asyncio.sleep(5)
            await check_no_skill_credentials(sandbox)
            result = await sandbox.commands.run(f"test ! -e {skill_cache_dir([slow])}")
            assert result.exit_code == 0
        else:
            cases = [
                {**declaration, "version": fixture["versions"][1]},
                {**declaration, "provider": "fixture_other"},
                {**declaration, "source": "fixture://other"},
            ]
            roots = {skill_cache_dir([declaration])}
            for source in cases:
                await prepare_agent_runtime_skill_cache(sandbox, [source])
                roots.add(skill_cache_dir([source]))
                content = (ROOT / ("version-2.md" if source["version"] == fixture["versions"][1] else "version-1.md")).read_bytes()
                installed = await check_installed_skill(sandbox, source, name=source["name"], expected=content)
                assert installed["declaration"]["provider"] == source["provider"]
                assert installed["declaration"]["source"] == source["source"]
                assert installed["version"] == source["version"]
            assert len(roots) == 4, "provider, source or version collided in the cache"
            await check_refused_sources(sandbox, [
                ({**declaration, "provider": "not-installed"}, None),
                ({**declaration, "config": {"credential_name": "not-authorized"}}, "SKILL_ACCESS_REFUSED"),
                ({**declaration, "source": "fixture://missing"}, "AGENT_RUNTIME_SKILL_CACHE_FAILED"),
                ({**declaration, "source": "fixture://escape"}, "AGENT_RUNTIME_SKILL_CACHE_FAILED"),
            ])
            await check_cancelled_acquisition(sandbox, {**declaration, "source": "fixture://slow"})
        await check_no_skill_credentials(sandbox)
        print(json.dumps({"contract": kind, "sandbox_id": box_id, "state": "PASS"}), flush=True)
    finally:
        provider.fetch = original_fetch
        await sandbox.close()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4])))
