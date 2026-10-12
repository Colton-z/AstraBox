"""Skill acquisition checks against a real sandbox and a native source fixture.

Bindings supply sources and expected bytes from their supplier's own records.
The checks use the production installer and real sandbox files, including its
credential cleanup and publication boundary. They do not substitute a sandbox
or a content provider.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable, Sequence

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.skill_sources import (
    normalize_skills,
    prepare_agent_runtime_skill_cache,
    skill_cache_dir,
)
from astrabox.seams.sandbox import SandboxHandle
from astrabox.seams.skills import SkillDescriptor


async def check_installed_skill(
    sandbox: SandboxHandle, source: SkillDescriptor, *, name: str, expected: bytes,
) -> dict:
    """Compare installed content, executable mode and provenance with source facts."""
    root = skill_cache_dir([source])
    assert await sandbox.files.read_bytes(f"{root}/{name}/SKILL.md") == expected
    entry = (await sandbox.files.stat([f"{root}/{name}/scripts/marker"]))[f"{root}/{name}/scripts/marker"]
    assert entry.mode & 0o111, "the supplier's executable bit did not reach the installed skill"
    receipt = json.loads(await sandbox.files.read_bytes(f"{root}/.skill-sources.json"))
    assert len(receipt) == 1 and receipt[0]["version"] and receipt[0]["content_id"]
    return receipt[0]


async def check_refused_sources(
    sandbox: SandboxHandle, sources: Sequence[tuple[SkillDescriptor, str | None]],
) -> None:
    """Refusals cannot publish a declaration or leave invocation credentials."""
    for source, code in sources:
        root = None
        try:
            normalize_skills([source])
            root = skill_cache_dir([source])
        except (APIError, ValueError):
            pass
        try:
            await prepare_agent_runtime_skill_cache(sandbox, [source])
        except APIError as exc:
            if code:
                assert exc.code == code, f"expected refusal {code}, got {exc.code}"
        except ValueError:
            assert code is None
        else:
            raise AssertionError("the invalid source was accepted")
        if root:
            result = await sandbox.commands.run(f"test ! -e {shlex.quote(root)}")
            assert result.exit_code == 0, "failed acquisition published a cache declaration"
        await check_no_skill_credentials(sandbox)


async def check_no_skill_credentials(sandbox: SandboxHandle) -> None:
    result = await sandbox.commands.run(
        "find /tmp -maxdepth 1 -type d -name 'astrabox-skills-*' -print"
    )
    assert result.exit_code == 0 and not result.stdout.strip(), "skill staging or credentials survived acquisition"


async def check_cancelled_acquisition(
    sandbox: SandboxHandle, source: SkillDescriptor,
    *, started: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Cancel after the supplier signals an active fetch; no content may publish.

    The binding's slow source writes ``fetch-started`` into its private
    directory, then holds the fetch open so cancellation occurs during actual
    acquisition with its temporary credential present.
    """
    task = asyncio.create_task(prepare_agent_runtime_skill_cache(sandbox, [source]))
    try:
        async with asyncio.timeout(30):
            while True:
                if task.done():
                    await task
                    raise AssertionError("the held source completed before cancellation")
                if started is None:
                    result = await sandbox.commands.run(
                        "find /tmp -maxdepth 3 -path '/tmp/astrabox-skills-*/private-*/fetch-started' -print"
                    )
                    active = bool(result.stdout.strip())
                else:
                    active = await started()
                if active:
                    break
                await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("skill acquisition swallowed cancellation")
        result = await sandbox.commands.run(f"test ! -e {shlex.quote(skill_cache_dir([source]))}")
        assert result.exit_code == 0, "cancelled acquisition published a completion marker"
        await check_no_skill_credentials(sandbox)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
