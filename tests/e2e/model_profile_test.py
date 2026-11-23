"""What the model gateway states about a model reaches the engine's own configuration."""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

import httpx
import pytest

from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    create_session,
    get_admin_session_detail,
    poll_until_agent_ready,
    run_terminal,
    send_and_follow,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e


def _gateway_facts(model: str) -> dict[str, Any]:
    """LiteLLM's own ``/model_group/info`` row for ``model``, read with its admin key."""

    program = """
import json,os,sys,httpx
from astrabox.providers.model import LiteLLMModelEndpointProvider
base=LiteLLMModelEndpointProvider.server_side_base_url().rstrip('/')
response=httpx.get(base+'/model_group/info',params={'model_group':sys.argv[1]},
                   headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']},timeout=15)
response.raise_for_status()
rows=[row for row in response.json()['data'] if row.get('model_group')==sys.argv[1]]
print(json.dumps(rows))
"""
    rows = json.loads(subprocess.check_output(
        ["docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE), "python", "-c", program, model],
        text=True,
    ))
    assert len(rows) == 1, f"the gateway describes {model!r} {len(rows)} times"
    return rows[0]


def _box_text(client: httpx.Client, sid: str, command: str) -> str:
    stdout, stderr, code, events = run_terminal(client, sid, command)
    assert code == 0, (command, stderr, events)
    return stdout


def _input(facts: dict[str, Any]) -> list[str]:
    return ["text", "image"] if facts.get("supports_vision") is True else ["text"]


def _codex(client: httpx.Client, sid: str, model: str, facts: dict[str, Any]) -> None:
    home = str((get_admin_session_detail(client, sid).get("runtime_identity") or {}).get("home_dir") or "")
    assert home, "the Codex conversation has no home"
    catalog = json.loads(_box_text(client, sid, "cat " + shlex.quote(f"{home}/.codex/models.json")))
    entries = [entry for entry in catalog["models"] if entry.get("slug") == model]
    assert len(entries) == 1, f"Codex's catalog names {model!r} {len(entries)} times"
    entry = entries[0]
    assert len(catalog["models"]) > 1, "the gateway model replaced Codex's bundled catalog"
    assert entry.get("context_window") == facts.get("max_input_tokens"), entry
    assert entry.get("input_modalities") == _input(facts), entry
    efforts = facts.get("supported_reasoning_efforts")
    levels = [level["effort"] for level in entry.get("supported_reasoning_levels") or []]
    assert levels == (list(efforts) if efforts is not None else []), entry


def _pi(client: httpx.Client, sid: str, model: str, facts: dict[str, Any]) -> None:
    home = str((get_admin_session_detail(client, sid).get("runtime_identity") or {}).get("home_dir") or "")
    assert home, "the Pi conversation has no home"
    config = json.loads(_box_text(client, sid, "cat " + shlex.quote(f"{home}/.pi/agent/models.json")))
    entries = [entry for provider in config["providers"].values() for entry in provider["models"]]
    assert [entry["id"] for entry in entries] == [model], entries
    entry = entries[0]
    assert entry.get("contextWindow") == facts.get("max_input_tokens"), entry
    assert entry.get("maxTokens") == facts.get("max_output_tokens"), entry
    assert entry.get("input") == _input(facts), entry
    efforts = facts.get("supported_reasoning_efforts")
    if efforts is None:
        assert "reasoning" not in entry and "thinkingLevelMap" not in entry, entry
        return
    assert entry.get("reasoning") is bool(efforts), entry
    sent = {value for value in (entry.get("thinkingLevelMap") or {}).values() if value}
    assert sent == set(efforts), entry


def _claude_code(client: httpx.Client, sid: str, model: str, facts: dict[str, Any]) -> None:
    # The CLI is launched with these, so they are read from its own process
    # after a turn; ANTHROPIC_MODEL proves the environment read is the CLI's.
    user = str((get_admin_session_detail(client, sid).get("runtime_identity") or {}).get("linux_user") or "")
    assert user, "the Claude conversation has no account"
    result = send_and_follow(client, sid, "Reply with the single word OK.", until="end")
    assert result.error is None, result.error
    listing = _box_text(
        client,
        sid,
        f"for p in $(pgrep -u {shlex.quote(user)}); do tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null; done"
        " | grep -E '^(ANTHROPIC_MODEL|CLAUDE_CODE_MAX_CONTEXT_TOKENS|CLAUDE_CODE_MAX_OUTPUT_TOKENS)=' | sort -u",
    )
    env = dict(line.split("=", 1) for line in listing.splitlines() if line)
    assert env.get("ANTHROPIC_MODEL") == model, listing
    for name, key in (
        ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "max_input_tokens"),
        ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "max_output_tokens"),
    ):
        stated = facts.get(key)
        assert env.get(name) == (str(stated) if stated else None), (name, listing)


def test_the_gateway_model_facts_reach_the_engine_configuration(e2e_client: httpx.Client) -> None:
    """The window, output cap, input kinds and effort levels the gateway states are the engine's."""

    profile = current_profile()
    checks = {"codex": _codex, "pi": _pi, "claude_code": _claude_code}
    if profile["engine_kind"] not in checks:
        pytest.skip(f"{profile['engine_kind']} models come from the harness's own catalog")
    facts = _gateway_facts(profile["model"])
    sid = str(create_session(e2e_client)["session_id"])
    poll_until_agent_ready(e2e_client, sid)
    checks[profile["engine_kind"]](e2e_client, sid, profile["model"], facts)
