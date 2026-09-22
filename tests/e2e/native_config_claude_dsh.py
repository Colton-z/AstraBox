"""Inspect native configuration at the running supplier boundary, without a model turn."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import uuid
from typing import Any

import httpx

from tests.e2e._sandbox_helpers import (
    OPEN_AGENTS,
    OPEN_SESSIONS,
    create_agent_variant_session,
    data,
    engine_kind,
    environment_with_tenancy,
    get_admin_session_detail,
    poll_until_agent_ready,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_terminal import _run_terminal
from tests.e2e.test_transcript_mirror import _query_mirror_docs


def _create(
    client: httpx.Client, options: dict[str, object], *, environment_name: str | None = None,
) -> str:
    created = create_agent_variant_session(
        client, name_suffix="native-json", engine_options=options,
        environment_name=environment_name,
    )
    sid = str(created.get("session_id") or "")
    assert sid, created
    poll_until_agent_ready(client, sid)
    return sid


def verify_claude_native_configuration(client: httpx.Client) -> None:
    """Prove SDK env and resolved tracing config reach Claude, without a model or collector."""

    assert engine_kind() == "claude_code"
    # PATH is a non-secret SecretProvider input already present in the server.
    # Return only the expected complete header's digest, never the environment.
    expected_header_sha256 = subprocess.check_output(
        [
            "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
            "python3", "-c",
            "import hashlib,os; value=os.environ['PATH']; assert value; "
            "print(hashlib.sha256(('Authorization='+value).encode()).hexdigest())",
        ],
        text=True, timeout=20,
    ).strip()
    assert len(expected_header_sha256) == 64, "server PATH probe returned no SHA-256 digest"
    source_name = environment_with_tenancy(client, "conversation")
    environments = data(client.get("/api/v1/admin/environments"))
    source = next(row for row in environments if row.get("name") == source_name)
    schema = data(client.get("/api/v1/admin/environment-schema"))
    fields = {field["key"] for field in schema["fields"]}
    environment = {key: value for key, value in source.items() if key in fields}
    environment_name = "native-tracing-config-" + uuid.uuid4().hex
    environment.update({
        "name": environment_name,
        "display_name": "Claude native tracing configuration probe (no model turn)",
        "enabled": True,
        # Replace both credential forms; an admin read's masked key cannot be
        # cloned. This test proves configuration delivery, not model auth.
        "provider_access": {
            **(source.get("provider_access") or {}),
            "api_key": "native-config-probe-not-a-model-credential",
            "api_key_secret_name": "",
        },
        "tracing": {
            "enabled": True,
            "endpoint": "https://collector.invalid",
            "auth_token_secret_name": "path",
            "signals": ["metrics"],
        },
    })
    environment_url = f"/api/v1/admin/environments/{environment_name}"
    data(client.put(environment_url, json=environment))
    # No environment-delete API exists. Failures retain this unique preset;
    # success retires its dependants before disabling the preset.
    print(f"Native configuration probe environment (retained on failure): {environment_name}")
    marker = "claude_native_env_" + uuid.uuid4().hex
    variable = "ASTRABOX_CLAUDE_CONFIG_PROBE"
    sid = _create(
        client, {"sdk_options": {"env": {variable: marker}}},
        environment_name=environment_name,
    )
    detail = get_admin_session_detail(client, sid)
    variant_agent_id = str(detail.get("agent_id") or "")
    assert variant_agent_id in OPEN_AGENTS, detail
    identity = detail.get("runtime_identity") or {}
    assert identity.get("sandbox_tenancy") == "conversation", identity
    assert not identity.get("isolated_session_id"), identity
    workload_user = str(identity.get("linux_user") or "")
    assert workload_user, identity
    # SDK 0.2.152 SubprocessCLITransport starts these two exact protocol flags.
    # Return only known probe switches and a header digest; no raw credentials.
    probe = """
import hashlib
import json
from pathlib import Path
found = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        args = proc.joinpath('cmdline').read_bytes().split(b'\\0')
        if not any(args[i:i+2] == [b'--output-format', b'stream-json'] for i in range(len(args))):
            continue
        if not any(args[i:i+2] == [b'--input-format', b'stream-json'] for i in range(len(args))):
            continue
        executable = str(proc.joinpath('exe').resolve())
        values = dict(item.split(b'=', 1) for item in proc.joinpath('environ').read_bytes().split(b'\\0') if b'=' in item)
        value = values.get(b'ASTRABOX_CLAUDE_CONFIG_PROBE')
        if value is not None:
            switches = [b'CLAUDE_CODE_ENABLE_TELEMETRY', b'OTEL_EXPORTER_OTLP_ENDPOINT',
                        b'OTEL_EXPORTER_OTLP_PROTOCOL', b'OTEL_METRICS_EXPORTER',
                        b'OTEL_TRACES_EXPORTER', b'OTEL_LOGS_EXPORTER']
            found.append({
                'pid': int(proc.name), 'executable': executable, 'value': value.decode(),
                'tracing': {key.decode(): values.get(key, b'').decode() for key in switches},
                'header_sha256': hashlib.sha256(values.get(b'OTEL_EXPORTER_OTLP_HEADERS', b'')).hexdigest(),
            })
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
print(json.dumps(found))
"""
    # Conversation tenancy's terminal uses the box PTY, not the shared Agent's
    # sibling isolation session. Read procfs as the actual vendor workload UID.
    command = "runuser -u " + shlex.quote(workload_user) + " -- python3 -c " + shlex.quote(probe)
    stdout, stderr, code, events = _run_terminal(client, sid, command)
    assert code == 0, (stderr, events)
    processes = json.loads(stdout)
    matches = [process for process in processes if process["value"] == marker]
    assert len(matches) == 1, processes
    assert matches[0]["pid"] > 0 and matches[0]["executable"], matches
    assert matches[0]["tracing"] == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.invalid",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
    }, matches
    assert matches[0]["header_sha256"] == expected_header_sha256, (
        "SecretProvider PATH must reach native OTEL Authorization verbatim", matches
    )
    # Agent retirement still resolves its Environment. Keep that dependency
    # enabled until all test-owned dependants are confirmed deleted.
    data(client.delete(f"/api/v1/sessions/{sid}", timeout=60.0))
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
    OPEN_SESSIONS.remove(sid)
    data(client.delete(f"/api/v1/agents/{variant_agent_id}", timeout=60.0))
    assert client.get(f"/api/v1/agents/{variant_agent_id}").status_code == 404
    OPEN_AGENTS.remove(variant_agent_id)
    data(client.put(environment_url, json={**environment, "enabled": False}))
    print(f"Native configuration proved; no telemetry collection asserted. Disabled {environment_name}")


def verify_dsh_native_configuration(client: httpx.Client) -> None:
    """Read the native Session header produced after the supplier mounts its preset."""

    assert engine_kind() == "deepseek_harness"
    # Published 0.1.2-rc.1 agent-presets ships minimal and defaults to standard.
    # session/create resolves and mounts the preset before publishing the Session.
    sid = _create(client, {"session_create": {"agentPreset": "minimal"}})
    expected_model = str(get_admin_session_detail(client, sid).get("model_name") or "")
    assert expected_model, "the Environment resolved no model for this conversation"
    deadline = time.monotonic() + 30
    records: list[dict[str, Any]] = []
    while True:
        records = [json.loads(row["entry_json"]) for row in _query_mirror_docs(sid)]
        headers = [row for row in records if row.get("type") == "session"]
        models = [row for row in records if row.get("type") == "model/selection"]
        if (headers and models) or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    assert len(headers) == 1, {"session_id": sid, "native_records": records}
    assert headers[0].get("agentPreset") == "minimal", headers[0]
    assert headers[0].get("id"), headers[0]
    selections = [row for row in records if row.get("type") == "agent-preset/selected"]
    assert all(row["data"]["agentPreset"] == "minimal" for row in selections), selections
    # The supplier's own durable record of which model its next request uses.
    # `session/create` carries no model, so without an explicit selection this
    # conversation runs the image's default and the Environment's choice never
    # reaches the wire — the shape that reached a user as a rejected model name
    # dressed up as a lost engine connection.
    assert models, {"session_id": sid, "native_records": records}
    assert models[-1]["data"]["model"] == expected_model, models
    assert models[-1]["data"]["provider"] == "deepseek-official", models
