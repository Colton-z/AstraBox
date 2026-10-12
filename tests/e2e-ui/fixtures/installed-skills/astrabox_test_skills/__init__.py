"""A separately installed content supplier for the live Skills journey.

The fixture's native store is a mounted directory with versioned files and an
access credential. The supplier reads that store, authorizes every resolution
and writes real content through the sandbox's file API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from pathlib import Path

from astrabox.common.utils.errors import APIError
from astrabox.seams.skills import ResolvedSkillSource, SkillContentProvider, SkillSource

ROOT = Path("/opt/astrabox-skill-fixture")


class FixtureContent(SkillContentProvider):
    name = "fixture_content"

    def validate(self, source: SkillSource) -> None:
        if set(source.config) != {"credential_name"}:
            raise ValueError("fixture content requires only credential_name")
        if source.config["credential_name"] != "fixture-access":
            raise APIError(code="SKILL_ACCESS_REFUSED", message="fixture credential is not authorized", status_code=403)
        if source.source not in {"fixture://primary", "fixture://other", "fixture://escape", "fixture://missing", "fixture://slow"}:
            raise ValueError("unknown fixture skill source")

    def egress_hosts(self, source: SkillSource) -> tuple[str, ...]:
        return ()

    async def resolve(self, source, sandbox, private_dir):
        self.validate(source)
        # Read after authorization; this material never belongs to a source or receipt.
        credential = (ROOT / "access-key").read_bytes()
        if not credential:
            raise APIError(code="SKILL_ACCESS_REFUSED", message="fixture access is unavailable", status_code=403)
        await self.write_credential(sandbox, private_dir, "credential", credential)
        index = json.loads((ROOT / "versions.json").read_text())
        if source.version not in index:
            raise ValueError("unknown fixture version")
        content = (ROOT / index[source.version]).read_bytes()
        if hashlib.sha256(content).hexdigest() != source.version:
            raise ValueError("fixture source digest changed")
        return ResolvedSkillSource(source.version, source.version)

    async def fetch(self, source, resolved, sandbox, destination, private_dir):
        with (ROOT / "fetches.jsonl").open("a") as stream:
            stream.write(json.dumps({"provider": self.name, "source": source.source, "version": resolved.version}) + "\n")
        if source.source == "fixture://slow":
            await sandbox.files.write_bytes(f"{private_dir}/fetch-started", b"started")
            await asyncio.Event().wait()
        index = json.loads((ROOT / "versions.json").read_text())
        content = (ROOT / index[resolved.version]).read_bytes()
        await sandbox.files.write_bytes(f"{destination}/SKILL.md", content)
        await sandbox.files.write_bytes(f"{destination}/scripts/marker", b"#!/bin/sh\nprintf 'native-skill-script\\n'\n", mode=0o755)
        if source.source == "fixture://missing":
            await sandbox.files.remove([f"{destination}/SKILL.md"])
        if source.source == "fixture://escape":
            result = await sandbox.commands.run(
                f"ln -s /etc/passwd {shlex.quote(destination + '/outside')}"
            )
            assert result.exit_code == 0


class OtherContent(FixtureContent):
    name = "fixture_other"
