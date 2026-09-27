"""The scheduling probe admits its private Git host without opening shared policy."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.author_boundary import check_author_egress_hosts
from astrabox.core.service.orchestrator.environment_networking import parse_environment_networking


DRIVER = r"""
import assert from 'node:assert/strict';
import { startupSkillEnvironment } from './tests/e2e-ui/fixtures/startupSkillGate.ts';
const source = {
  name: 'shared', display_name: 'Shared', enabled: true, engine_kind: 'claude_code',
  networking: { type: 'limited', allowed_hosts: ['github.com'], allow_mcp_servers: false },
  sandbox_tenancy: 'agent', runtime_template_name: 'installed-image', response_only: 'omit',
};
const before = structuredClone(source);
const fields = Object.keys(source).filter(key => key !== 'response_only');
const fixture = startupSkillEnvironment(source, fields, 'test-owned',
  'http://10.42.0.1:32123/repo.git@0123456789abcdef#startup-gate');
assert.deepEqual(source, before, 'the shared Environment must retain its policy');
assert.equal(fixture.name, 'test-owned');
assert.equal(fixture.display_name, 'test-owned');
assert.equal(fixture.sandbox_tenancy, 'agent');
assert.equal(fixture.runtime_template_name, 'installed-image');
assert.equal('response_only' in fixture, false);
assert.deepEqual(fixture.networking, {
  type: 'limited', allowed_hosts: ['github.com', '10.42.0.1'], allow_mcp_servers: false,
});
console.log(JSON.stringify({ before: source.networking, after: fixture.networking }));
"""


def test_startup_skill_host_is_admitted_only_in_its_owned_environment(
    node_toolchain_env: dict[str, str],
) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", DRIVER],
        cwd=Path(__file__).resolve().parents[1],
        env=node_toolchain_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    with pytest.raises(APIError, match="private address"):
        check_author_egress_hosts(parse_environment_networking(evidence["before"]), ["10.42.0.1"])
    assert check_author_egress_hosts(
        parse_environment_networking(evidence["after"]), ["10.42.0.1"]
    ) == []
    with pytest.raises(APIError, match="private address"):
        check_author_egress_hosts(parse_environment_networking(evidence["after"]), ["10.42.0.2"])
