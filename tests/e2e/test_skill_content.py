"""Installed providers deliver native source facts to real conversation Skills."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    data, environment_with_tenancy, get_admin_session_detail, poll_until_agent_ready,
    release_agent, release_session, run_terminal,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = [pytest.mark.e2e, pytest.mark.backend_restart, pytest.mark.xdist_group("backend-restart")]


@pytest.fixture(scope="module")
def skill_supplier(e2e_base_url, e2e_auth_headers):
    fixtures = Path(__file__).parents[1] / "e2e-ui" / "fixtures"
    sys.path.insert(0, str(fixtures))
    spec = importlib.util.spec_from_file_location("installed_skills_backend", fixtures / "installed_skills_backend.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # sshd checks every authorized-key ancestor; /tmp is world-writable.
    root = Path(tempfile.mkdtemp(prefix=".astrabox-skill-supplier-", dir=Path.home())) / "fixture"
    receipt = module.start(require_service_container(SERVER_CONTAINER_HANDLE), e2e_base_url, root)
    fields = (root / "wrong.pub").read_text().split()
    receipt["wrong_known_hosts"] = receipt["known_hosts"].split()[0] + " " + " ".join(fields[:2])
    changed = {}
    try:
        with httpx.Client(base_url=e2e_base_url, headers=e2e_auth_headers, timeout=30) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    if client.get("/healthz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "the fixture server did not become healthy"
                time.sleep(0.5)
            name = environment_with_tenancy(client, "conversation")
            runtime_profiles = data(client.get("/api/v1/admin/runtime-profiles"))
            environment = next(item for item in runtime_profiles if item["name"] == name)
            networking = environment.get("networking") or {}
            changed = {"name": name, "payload": environment}
            data(client.put(f"/api/v1/admin/runtime-profiles/{name}", json={
                **environment,
                "networking": {"type": "limited", **networking, "allowed_hosts": list(dict.fromkeys([*(networking.get("allowed_hosts") or []), receipt["host"]]))},
            }))
            yield receipt
    finally:
        try:
            if changed:
                with httpx.Client(base_url=e2e_base_url, headers=e2e_auth_headers, timeout=30) as client:
                    data(client.put(f"/api/v1/admin/runtime-profiles/{changed['name']}", json=changed["payload"]))
        finally:
            module.restore(receipt)
            sys.path.remove(str(fixtures))


def _exercise(client, fixture, source, kind, deadline):
    profile = current_profile()
    # Native supplier roots, pinned in docs/maintainers/engine-extension-contract.md.
    native_root = {
        "claude_code": ".claude/skills",
        "codex": ".agents/skills",
        "pi": ".agents/skills",
        "deepseek_harness": ".agents/skills",
        "hermes": ".hermes/skills/astrabox",
    }[profile["engine_kind"]]
    original = data(client.get(f"/api/v1/agents/{profile['agent_id']}"))
    agent = data(client.post("/api/v1/agents", json={
        "name": f"skill-source-{uuid.uuid4().hex[:10]}",
        "model": original["model"], "engine_options": original.get("engine_options") or {},
        "runtime_profile_name": environment_with_tenancy(client, "conversation"),
        "prepared_capacity": 0, "skills": [source],
    }))
    release_agent(agent["agent_id"])
    stored = client.get(f"/api/v1/agents/{agent['agent_id']}")
    assert data(stored)["skills"] == [source]
    assert "PRIVATE KEY" not in stored.text
    expected = (Path(fixture["root"]) / "store" / "version-1.md").read_text()
    boxes = []
    for attempt in range(2):
        session = data(client.post(f"/api/v1/agents/{agent['agent_id']}/conversations", json={}))
        sid = session["session_id"]
        release_session(sid)
        poll_until_agent_ready(client, sid)
        detail = get_admin_session_detail(client, sid)
        boxes.append(detail["sandbox_id"])
        # Conversation tenancy runs the image's agent account; the management
        # terminal's HOME belongs to its separate platform account.
        out, err, rc, _ = run_terminal(client, sid,
            'home="$(getent passwd agent | cut -d: -f6)"; test -n "$home" && '
            f'skill="$home/{native_root}/native-marker/SKILL.md" && '
            'cat "$skill" && "$(dirname "$skill")/scripts/marker"')
        assert rc == 0, (out, err)
        assert expected.strip() in out and "native-skill-script" in out
        if attempt == 0:
            script = Path(__file__).with_name("skill_content_conformance.py").read_text()
            result = subprocess.run([
                "docker", "exec", "-i", fixture["name"], "python", "-", detail["sandbox_id"], kind,
                json.dumps(source), json.dumps({"versions": fixture["versions"], "wrong_known_hosts": fixture["wrong_known_hosts"]}),
            ], input=script, capture_output=True, text=True, timeout=max(1, deadline - time.monotonic()))
            print(result.stdout, flush=True)
            assert result.returncode == 0, result.stderr[-4000:]
    assert len(set(boxes)) == 2, "reinstallation was not checked in a fresh conversation sandbox"


def test_a_named_ssh_key_installs_a_private_skill(e2e_client, skill_supplier, live_test_deadline):
    source = {"provider": "git", "source": skill_supplier["ssh_source"],
              "version": skill_supplier["revision"], "path": "skills/native-marker", "name": "native-marker",
              "config": {"deploy_key_secret_name": "fixture-skill-deploy", "known_hosts": skill_supplier["known_hosts"]}}
    _exercise(e2e_client, skill_supplier, source, "ssh", live_test_deadline)


def test_an_installed_content_provider_supplies_a_skill(e2e_client, skill_supplier, live_test_deadline):
    source = {"provider": "fixture_content", "source": "fixture://primary",
              "version": skill_supplier["versions"][0], "name": "native-marker",
              "config": {"credential_name": "fixture-access"}}
    _exercise(e2e_client, skill_supplier, source, "content", live_test_deadline)
