"""A Skill from a private HTTPS repository arrives through a Vault credential.

The repository is private, so a Skill whose content reaches the engine's Skill
directory was downloaded with the HTTP Basic credential the Agent's Vault holds.
The expected content is read from GitHub with the same token, not written here,
and the token itself must stay out of the Agent record and the sandbox: the
egress sidecar adds it outside the box.
"""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    data,
    environment_with_tenancy,
    poll_until_agent_ready,
    release_agent,
    release_session,
    run_terminal,
)

pytestmark = pytest.mark.e2e

REPOSITORY = "Colton-z/astrabox-e2e-private-skill"
REVISION = "aaa005dfa5d0195a13af439b9aebc5257e8597f4"
SKILL_PATH = "skills/private-marker"
#: An operator private input: a GitHub token that can read REPOSITORY. A
#: fine-grained token with read-only Contents access to it is the least it needs.
TOKEN_FILE = os.environ.get(
    "ASTRABOX_E2E_PRIVATE_SKILL_TOKEN_FILE",
    str(Path.home() / "astrabox-private" / "github-private-skill.token"),
)


def _repository_token() -> str:
    path = Path(TOKEN_FILE)
    if not path.is_file():
        pytest.fail(
            f"operator private input {path} is missing: a read-only token for {REPOSITORY}"
        )
    token = path.read_text(encoding="utf-8").strip()
    assert token, f"{path} is empty"
    return token


def _skill_text_from_github(token: str) -> str:
    response = httpx.get(
        f"https://api.github.com/repos/{REPOSITORY}/contents/{SKILL_PATH}/SKILL.md",
        params={"ref": REVISION},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()
    return base64.b64decode(response.json()["content"]).decode("utf-8")


def test_a_private_skill_arrives_through_the_agents_vault(e2e_client: httpx.Client) -> None:
    token = _repository_token()
    expected = _skill_text_from_github(token)
    marker = expected.strip().splitlines()[-1].strip()
    assert marker.startswith("PRIVATE-SKILL-"), expected

    profile = current_profile()
    source = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    vault = data(e2e_client.post("/api/v1/admin/vaults", json={
        "display_name": f"private-skill-{uuid.uuid4().hex[:8]}",
        "metadata": {},
    }))
    vault_id = vault["vault_id"]
    agent_id = ""
    try:
        data(e2e_client.post(f"/api/v1/admin/vaults/{vault_id}/credentials", json={
            "display_name": "Private Skill repository",
            "auth": {
                "type": "http_basic",
                "url": f"https://github.com/{REPOSITORY}.git",
                "username": "x-access-token",
                "password": token,
            },
        }))
        skill = f"https://github.com/{REPOSITORY}.git@{REVISION}#{SKILL_PATH}"
        agent = data(e2e_client.post("/api/v1/agents", json={
            "name": f"private-skill-{uuid.uuid4().hex[:10]}",
            "model": source["model"],
            "engine_options": source.get("engine_options") or {},
            "runtime_profile_name": environment_with_tenancy(e2e_client, "conversation"),
            "prepared_capacity": 0,
            "skills": [skill],
        }))
        agent_id = agent["agent_id"]
        release_agent(agent_id)
        data(e2e_client.put(
            f"/api/v1/admin/agents/{agent_id}/credential-vaults",
            json={"vault_ids": [vault_id]},
        ))
        stored = e2e_client.get(f"/api/v1/agents/{agent_id}")
        assert token not in stored.text
        assert data(stored)["skills"] == [skill]

        session = data(e2e_client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))
        sid = session["session_id"]
        release_session(sid)
        poll_until_agent_ready(e2e_client, sid)

        # The platform installs an Agent's Skills in its runtime cache under
        # /opt/conversation-runtime; an engine may also link them under $HOME.
        out, err, rc, _ = run_terminal(
            e2e_client, sid,
            'skill="$(find -L "$HOME" /opt/conversation-runtime -path "*/private-marker/SKILL.md" '
            '-type f -print -quit 2>/dev/null)"; '
            'test -n "$skill" && cat "$skill"',
        )
        assert rc == 0, (out, err, rc)
        assert marker in out, (out, err)
        # Every GitHub token starts with its type (github_pat_, ghp_, gho_, ...);
        # the box is searched for that, so the full value is never sent into it.
        kind = token[: token.index("_") + 1]
        out, err, rc, _ = run_terminal(
            e2e_client, sid,
            f'grep -rIl "{kind}" "$HOME" /opt/conversation-runtime 2>/dev/null | head -5; true',
        )
        assert rc == 0 and not out.strip(), (out, err, rc)
    finally:
        if agent_id:
            e2e_client.put(
                f"/api/v1/admin/agents/{agent_id}/credential-vaults", json={"vault_ids": []}
            )
        e2e_client.delete(f"/api/v1/admin/vaults/{vault_id}")
