"""Verified enterprise identities enter without acquiring resource permissions."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def test_environment_entry_policy_preserves_identity_and_resource_boundaries() -> None:
    """Default members and two corporate replicas decide entry from verified identity."""
    fixture = json.loads(Path(os.environ["ASTRABOX_E2E_ENVIRONMENT_ENTRY_FIXTURE"]).read_text())
    enterprise, replica = fixture["enterprise_urls"]
    members = fixture["members_url"]
    tokens = fixture["tokens"]

    def employee_rows(table: str) -> int:
        user_id = fixture["employee_id"].replace("'", "''")
        value = subprocess.check_output([
            "docker", "exec", fixture["postgres_container"], "psql", "-XAtq", "-U", "postgres",
            "-d", fixture["database"], "-c", f"SELECT count(*) FROM {table} WHERE user_id='{user_id}'",
        ], text=True, timeout=20)
        return int(value.strip())

    def call(base: str, path: str, actor: str | None = "employee", *, body=None, extra=None):
        headers = {"Authorization": "Bearer " + tokens[actor]} if actor else {}
        headers.update(extra or {})
        return httpx.request("POST" if body is not None else "GET", base + path,
                             headers=headers, json=body, timeout=20)

    def data(response: httpx.Response):
        assert response.status_code == 200, response.text
        return response.json()["data"]

    # The fixture signs identities independently of the application and has no
    # profile or member row for this employee. Member-list mode must still deny.
    assert employee_rows("user_profiles") == 0
    assert employee_rows("environment_members") == 0
    assert call(members, "/api/v1/agents").status_code == 403
    assert data(call(members, "/api/v1/environments")) == []
    assert call(members, "/api/v1/agents", "member").status_code == 200
    for base in (enterprise, replica):
        current = data(call(base, "/api/v1/environments/current"))
        assert current["name"] == fixture["environment_name"]
        assert [row["name"] for row in data(call(base, "/api/v1/environments"))] == [current["name"]]
        assert call(base, "/api/v1/agents").status_code == 200
        assert data(call(base, "/api/v1/user/current"))["user_id"] == fixture["employee_id"]
        assert "admin" not in data(call(base, "/api/v1/user/current"))["roles"]
        assert call(base, "/api/v1/admin/environments").status_code == 403
        # Request headers cannot turn a deployment into another Environment.
        assert data(call(base, "/api/v1/environments/current", extra={
            "X-AstraBox-Environment": "a-different-environment",
        }))["name"] == current["name"]
        mcp = call(base, "/api/v1/mcp", body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        }, extra={"MCP-Protocol-Version": "2025-03-26"})
        assert mcp.status_code == 200, mcp.text
        assert "tools" in mcp.json()["result"]

    # Metadata-only primary creation verifies the Assistant owner ACL without
    # creating a conversation, borrowing a sandbox or asking a model anything.
    primary = data(call(enterprise, "/api/v1/assistants/primary", body={"display_name": "Employee"}))
    assert data(call(replica, "/api/v1/assistants/primary", body={"display_name": "Employee"}))["assistant_id"] == primary["assistant_id"]
    assert call(enterprise, "/api/v1/assistants/" + primary["assistant_id"], "other_employee").status_code in (403, 404)

    key = call(enterprise, "/api/v1/mcp-tokens", body={"name": "Entry policy", "scope": "read"})
    assert key.status_code == 201, key.text
    tokens["issued_key"] = key.json()["data"]["secret"]
    for base, expected in [(enterprise, 200), (replica, 200), (members, 403)]:
        protocol = call(base, "/api/v1/mcp", "issued_key", body={
            "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
        }, extra={"MCP-Protocol-Version": "2025-03-26"})
        assert protocol.status_code == expected, protocol.text

    for actor, expected in [(None, 401), ("expired", 401), ("forged", 401),
                            ("outsider", 403), ("directory_failure", 503)]:
        for path in ("/api/v1/agents", "/api/v1/environments/current"):
            response = call(enterprise, path, actor)
            assert response.status_code == expected, (actor, path, response.text)
        protocol = call(enterprise, "/api/v1/mcp", actor, body={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, extra={"MCP-Protocol-Version": "2025-03-26"})
        assert protocol.status_code == expected, (actor, protocol.text)
    assert call(enterprise, "/api/v1/environments", "directory_failure").status_code == 503
    assert data(call(enterprise, "/api/v1/environments", None)) == []
    admin = data(call(enterprise, "/api/v1/admin/environments/" + fixture["environment_name"], "admin"))
    assert fixture["employee_id"] not in admin["member_user_ids"]
    assert employee_rows("environment_members") == 0
