"""Interrupted fault cleanup preserves evidence and fences its one-field repair."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ORIGINAL = {
    "session_id": "fault-session", "agent_id": "fault-agent", "sandbox_id": "original-box",
    "isolated_session_id": "original-isolation", "terminal_isolated_session_id": "terminal-isolation",
    "state": "before-reconciliation",
}


def _node(source: str, environment: dict[str, str]) -> str:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=REPO,
        env=environment, text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize("status", ["passed", "interrupted", "failed", "timedOut"])
def test_fault_evidence_precedes_repair(status: str, node_toolchain_env: dict[str, str]) -> None:
    _node(r"""
import assert from 'node:assert/strict';
import { finishNoSandboxFault } from './tests/e2e-ui/fixtures/noSandboxFault.ts';
const status = STATUS;
const original = ORIGINAL;
const session = { ...original, sandbox_id: undefined, state: 'reconciled' };
const snapshot = { verdict: 'FAILED', turn_recovery_phase: 'SETTLED' };
const journal = [{ event: 'verdict-recorded' }];
let attachment;
let repaired = false;
await finishNoSandboxFault({
  status, original,
  capture: () => ({ session, snapshot, journal }),
  attach: async body => {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(repaired, false);
    attachment = JSON.parse(body);
  },
  restore: doc => {
    assert.ok(attachment, 'repair must await the evidence write');
    assert.deepEqual(doc, original);
    repaired = true;
  },
});
assert.equal(repaired, ['passed', 'interrupted'].includes(status));
if (repaired) {
  assert.equal(attachment.before_repair.session.sandbox_id, undefined);
  assert.equal(attachment.before_repair.session.state, 'reconciled');
  assert.deepEqual(attachment.before_repair.snapshot, snapshot);
  assert.deepEqual(attachment.before_repair.journal, journal);
}
""".replace("STATUS", json.dumps(status)).replace("ORIGINAL", json.dumps(ORIGINAL)), node_toolchain_env)


def test_attachment_failure_keeps_the_fault(node_toolchain_env: dict[str, str]) -> None:
    _node(r"""
import assert from 'node:assert/strict';
import { finishNoSandboxFault } from './tests/e2e-ui/fixtures/noSandboxFault.ts';
await assert.rejects(finishNoSandboxFault({
  status: 'interrupted', original: { session_id: 's' }, capture: () => ({}),
  attach: async () => { throw new Error('evidence disk full'); },
  restore: () => assert.fail('must not mutate without evidence'),
}), /evidence disk full/);
await finishNoSandboxFault({
  status: 'interrupted', original: undefined,
  capture: () => assert.fail('fault was never injected'),
  attach: async () => assert.fail('fault was never injected'),
  restore: () => assert.fail('fault was never injected'),
});
""", node_toolchain_env)


def _repair_sql(environment: dict[str, str], matched_rows: str = "{}\n") -> str:
    # Replace process/container discovery only; run the oracle's actual SQL builder.
    source = r"""
import { registerHooks } from 'node:module';
globalThis.repairRows = MATCHED_ROWS;
const asModule = source => ({ url: 'data:text/javascript,' + encodeURIComponent(source), shortCircuit: true });
registerHooks({ resolve(specifier, context, next) {
  if (specifier === './serviceContainer') return asModule(`
    export const POSTGRES_CONTAINER_HANDLE = {};
    export const requireServiceContainer = () => 'test-postgres';
  `);
  if (specifier === 'node:child_process') return asModule(`
    export const execFileSync = () => { throw new Error('unexpected whole-row operation'); };
    export const spawnSync = (command, args) => {
      if (command !== 'docker') throw new Error(command);
      console.log(JSON.stringify(args[args.indexOf('-c') + 1]));
      return {status: 0, stdout: globalThis.repairRows, stderr: ''};
    };
  `);
  return next(specifier, context);
}});
const { restoreSessionSandboxPointer } = await import('./tests/e2e-ui/fixtures/dbOracle.ts');
restoreSessionSandboxPointer(ORIGINAL);
""".replace("ORIGINAL", json.dumps(ORIGINAL)).replace("MATCHED_ROWS", json.dumps(matched_rows))
    return json.loads(_node(source, environment))


@pytest.mark.parametrize("matched_rows", ["", "{}\n{}\n"])
def test_repair_refuses_missing_or_ambiguous_placement(
    matched_rows: str, node_toolchain_env: dict[str, str],
) -> None:
    with pytest.raises(AssertionError, match="expected one unchanged placement"):
        _repair_sql(node_toolchain_env, matched_rows)


@pytest.mark.postgresql
@pytest.mark.parametrize("changed", [None, "sandbox_id", "agent_id", "isolated_session_id", "terminal_isolated_session_id"])
async def test_pointer_repair_preserves_reconciliation_and_refuses_rebinding(
    changed: str | None, node_toolchain_env: dict[str, str],
) -> None:
    import asyncpg

    from tests._postgresql_support import resolve_postgresql_test_url

    sql = _repair_sql(node_toolchain_env)
    current = {**ORIGINAL, "state": "reconciled", "verdict": "FAILED"}
    del current["sandbox_id"]
    if changed:
        current[changed] = "replacement"
    connection = await asyncpg.connect(
        resolve_postgresql_test_url().replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    try:
        # A connection-local table shadows the live store; no product rows are touched.
        await connection.execute("CREATE TEMP TABLE astrabox_documents (collection text, doc jsonb)")
        await connection.execute(
            "INSERT INTO astrabox_documents VALUES ('sessions', $1::jsonb)", json.dumps(current),
        )
        rows = await connection.fetch(sql)
        after = json.loads(await connection.fetchval("SELECT doc::text FROM astrabox_documents"))
        if changed:
            assert rows == []
            assert after == current
        else:
            assert len(rows) == 1
            assert after == {**current, "sandbox_id": ORIGINAL["sandbox_id"]}
    finally:
        await connection.close()
