/**
 * Daily Assistant activity retains the same workspace box past the next night.
 *
 * Drive a real first turn, shorten the supplier lease to fifteen minutes, and
 * evict only the cached host runtime so its old expiry cannot suppress renewal.
 * A real morning turn must use the same box and restore at least a day of lease.
 * The supplier expiry, not a rewritten Session field, is the acceptance oracle.
 * Keep this cold Assistant journey in the one-worker serial group and preserve
 * the suite's 180-second whole-test budget.
 */
import { execFileSync } from 'node:child_process';

import { test, expect } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const ASSISTANT_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_ASSISTANT_TURN_TIMEOUT_MS', 120_000);
// How long the platform may take to publish the renewal its own turn triggered.
const RENEWAL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_LEASE_RENEW_CONVERGE_MS', 60_000);

// The product bar, deliberately NOT env-tunable: the journey spans one night,
// and a harness that could lower this could lower it under the 4-hour
// conversation lease and make the defect pass.
const OVERNIGHT_MS = 24 * 60 * 60 * 1_000;
// `sandbox_lease_seconds` (settings.py:520) — the conversation lease the
// activity renewal writes. The compression must land below it, or the product's
// renew would short-circuit and the morning turn would prove nothing.
const CONVERSATION_LEASE_MS = 4 * 60 * 60 * 1_000;
const COMPRESSED_LEASE_S = 900;

const NO_TOOLS_PROMPT = '请简短回复一句话，不要使用工具。';

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

/** The box's own expiry, in epoch milliseconds. A box with none is a defect here. */
function expiryMillis(record: Record<string, unknown>, label: string): number {
  const raw = String(record.expires_at || '').trim();
  expect(raw, `${label} must expose expires_at`).not.toEqual('');
  const parsed = Date.parse(raw);
  expect(Number.isFinite(parsed), `${label} expires_at must be an ISO timestamp`).toBe(true);
  return parsed;
}

/** How much of the box's life is left, read at the moment of the call. */
function remainingMs(record: Record<string, unknown>, label: string): number {
  return expiryMillis(record, label) - Date.now();
}

function hours(ms: number): string {
  return `${(ms / 3_600_000).toFixed(2)}h`;
}

/**
 * Spend the seven days the budget cannot, by shortening the box's real lease.
 *
 * This calls the same vendor API `OpenSandboxProvider.renew` calls
 * (`open_sandbox/sandbox.py:1845-1856`), bypassing only that method's
 * never-shorten short-circuit, which is the mechanism under compression. The
 * before/after expiries come from the provider seam's own `expires_at`, so the
 * caller can prove the shortening actually landed rather than assume it.
 */
function compressLeaseToMinutes(input: {
  backend: string;
  sandboxId: string;
  ttlSeconds: number;
}): { before: string | null; renewed: string | null; after: string | null } {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const program = `
import asyncio
import json
import sys
from datetime import timedelta

from opensandbox import SandboxManager

from astrabox.bootstrap import bootstrap
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.seams.sandbox import sandbox_for_name

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    provider = sandbox_for_name(given['backend'])
    before = await provider.expires_at(given['sandboxId'])
    assert before is not None, 'the workspace box has no scheduled expiry to compress'
    # provider.renew() refuses to shorten by design; go to the vendor call it
    # makes, which is what the seven real days would eventually expose anyway.
    manager = await SandboxManager.create(
        connection_config=provider._sdk_connection_config(provider._settings())
    )
    try:
        response = await manager.renew_sandbox(
            str(given['sandboxId']), timedelta(seconds=int(given['ttlSeconds']))
        )
    finally:
        await manager.close()
    after = await provider.expires_at(given['sandboxId'])
    print('E2E_LEASE_COMPRESSED=' + json.dumps({
        'before': before.isoformat(),
        'renewed': response.expires_at.isoformat() if response.expires_at else None,
        'after': after.isoformat() if after else None,
    }))

asyncio.run(main())
`;
  const raw = execFileSync('docker', ['exec', server, 'python', '-c', program, JSON.stringify(input)], {
    encoding: 'utf8',
    timeout: 60_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const result = raw.split('\n').find((line) => line.startsWith('E2E_LEASE_COMPRESSED='));
  expect(result, 'the lease compression worker must publish its result').toBeTruthy();
  return JSON.parse(result!.slice('E2E_LEASE_COMPRESSED='.length));
}

test('an Assistant used today still has its workspace box the next morning', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');

  const environmentName = await api.assistantEnvironmentName();
  expect(environmentName, 'an assistant environment must exist').not.toEqual('');
  const assistantModel = await api.assistantModelName(environmentName);

  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_overnight_${runId}`,
    environment_name: environmentName,
    model_config_override: { model_name: assistantModel },
  });
  assistantId = String(assistant.assistant_id || '');
  expect(assistantId, 'created assistant must have an id').not.toEqual('');

  const workspace = await api.waitForWorkspaceReady(assistantId);
  const boxId = String(workspace.current_sandbox_id || '').trim();
  expect(boxId, 'a READY workspace must name its sandbox').not.toEqual('');

  // ── Day one, the promise ────────────────────────────────────────────────
  // A brand-new Assistant carries the durable horizon. This is the baseline the
  // morning assertion is calibrated against, and it doubles as a configuration
  // gate: a deployment that tuned ASTRABOX_AGENT_SANDBOX_RENEW_TTL_SECONDS below
  // a day fails HERE with that reason, instead of failing the morning check for
  // a reason that has nothing to do with the defect.
  const fresh = await api.getSandbox(boxId);
  expect(String(fresh.state).toLowerCase(), 'a new workspace box must be running').toBe('running');
  const freshRemaining = remainingMs(fresh, 'new workspace box');
  expect(
    freshRemaining,
    `a new Assistant's workspace box must be guaranteed well past tonight ` +
      `(have ${hours(freshRemaining)}, need ${hours(OVERNIGHT_MS)}); if this deployment lowered ` +
      'ASTRABOX_AGENT_SANDBOX_RENEW_TTL_SECONDS, the overnight journey is not configured here',
  ).toBeGreaterThanOrEqual(OVERNIGHT_MS);

  // ── Day one, real use ───────────────────────────────────────────────────
  // The verdict below has to be about an Assistant somebody actually used, not
  // an untouched one, so this turn goes through the composer like a person's.
  const created = await api.startAssistantConversation(assistantId);
  const sessionId = String(created.session_id || '').trim();
  expect(sessionId, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(sessionId);
  const dayOneSession = await api.waitForSessionReady(sessionId);
  expect(
    String(dayOneSession.sandbox_id || '').trim(),
    'the conversation must run on the workspace box this spec measures',
  ).toEqual(boxId);

  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);
  const beforeDayOne = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, NO_TOOLS_PROMPT);
  await api.waitForAssistantMessageCount(sessionId, beforeDayOne, ASSISTANT_TURN_MS);

  // Record the initial horizon without requiring an unnecessary extension
  // while the box already has sufficient lease remaining.
  const afterUse = await api.getSandbox(boxId);
  const afterUseRemaining = remainingMs(afterUse, 'box after ordinary use');

  // ── Seven days ──────────────────────────────────────────────────────────
  const inventory = await platform.listSandboxes();
  const backend = String(inventory.backend || '').trim();
  expect(backend, 'the sandbox inventory must name its backend').not.toEqual('');
  const compression = compressLeaseToMinutes({
    backend,
    sandboxId: boxId,
    ttlSeconds: COMPRESSED_LEASE_S,
  });

  // The morning turn must land on a cold runtime, exactly as the real day ~6.96
  // does once the cached lease drops under the renew threshold. Without this the
  // cached 7-day value throttles the renewal away and the spec would go red
  // without ever reaching the code it is about.
  await api.adminEvictRuntime(sessionId);

  // ── Prove the compression landed before trusting any verdict built on it ──
  // Require actual supplier-side shortening before testing renewal; a long
  // unchanged lease would satisfy the overnight assertion without exercising it.
  const compressed = await api.getSandbox(boxId);
  expect(
    String(compressed.state).toLowerCase(),
    'compressing the lease must not disturb the running box',
  ).toBe('running');
  const compressedRemaining = remainingMs(compressed, 'compressed box');
  expect(
    compressedRemaining,
    `the seven days could not be compressed: the box still holds ${hours(compressedRemaining)} ` +
      `after a ${COMPRESSED_LEASE_S}s renew, so the control plane did not honour a shortening ` +
      `renew and this run proves nothing about the product (compression=${JSON.stringify(compression)})`,
  ).toBeLessThan(CONVERSATION_LEASE_MS);
  expect(
    compressedRemaining,
    'the compressed box must still be alive for the morning message',
  ).toBeGreaterThan(0);

  // ── Day eight, morning ──────────────────────────────────────────────────
  // A new morning is a new page load and real conversation activity.
  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);
  const beforeMorning = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, NO_TOOLS_PROMPT);
  const morningReply = await api.waitForAssistantMessageCount(
    sessionId,
    beforeMorning,
    ASSISTANT_TURN_MS,
  );

  // ── The judgement ───────────────────────────────────────────────────────
  let renewed: Record<string, unknown> = {};
  await expect.poll(async () => {
    renewed = await api.getSandbox(boxId);
    return remainingMs(renewed, 'box after the morning message');
  }, {
    timeout: RENEWAL_CONVERGE_MS,
    intervals: [1_000, 2_000, 5_000],
    message:
      'an Assistant answering in the morning must still have its box the next morning. A ' +
      `horizon of about ${hours(CONVERSATION_LEASE_MS)} means the renewal wrote ` +
      'sandbox_lease_seconds instead of the Assistant workspace horizon.',
  }).toBeGreaterThanOrEqual(OVERNIGHT_MS);

  // A rebuilt box is a different behaviour and must not be silently compared
  // against a horizon this spec never measured.
  const settled = await api.waitForSessionReady(sessionId);
  expect(
    String(settled.sandbox_id || '').trim(),
    'the morning message must be served by the same box, not a replacement built in its place',
  ).toEqual(boxId);
  expect(String(settled.last_error || '').trim()).toEqual('');
  const morningText = messageText(morningReply).trim();
  expect(morningText, 'the morning message must produce a reply').not.toEqual('');
  expect(
    morningText,
    'the morning message must still be a user-visible success',
  ).not.toMatch(/^API Error:\s*\d+\b/);

  await test.info().attach('assistant-overnight-horizons', {
    body: JSON.stringify({
      assistantId,
      sessionId,
      boxId,
      backend,
      overnightBarMs: OVERNIGHT_MS,
      conversationLeaseMs: CONVERSATION_LEASE_MS,
      compressedLeaseSeconds: COMPRESSED_LEASE_S,
      expiries: {
        fresh: fresh.expires_at,
        afterDayOneUse: afterUse.expires_at,
        compressed: compressed.expires_at,
        afterMorningMessage: renewed.expires_at,
      },
      remainingHours: {
        fresh: hours(freshRemaining),
        afterDayOneUse: hours(afterUseRemaining),
        compressed: hours(compressedRemaining),
        afterMorningMessage: hours(remainingMs(renewed, 'final read')),
      },
      compression,
      // Which idle policy this deployment ran under, so a reader can check for
      // themselves that no park or retention horizon was in play.
      idleAction: await platform.idleAction(),
    }),
    contentType: 'application/json',
  });
});
