/**
 * E2E: an Assistant's workspace files survive hibernate → wake on a fresh box.
 *
 * This is the durable half of what an Assistant is. `docs/domain-model.md` §2
 * defines it as "one owner's durable personal workspace" whose workspace row is
 * "the durable container (files + memory) that ephemeral sessions attach to",
 * running one sandbox at a time. Hibernate commits the workspace through the
 * storage seam and releases that box; wake creates another and prepares it from
 * the same medium. Destroying before that commit would make every sleep a
 * silent data loss.
 *
 * The two halves are asserted with different instruments on purpose:
 *
 * - **that its engine state still serves** — the same conversation completes a
 *   real turn before hibernate and another after the fresh box is ready;
 * - **that the files are still there** — a marker file placed and read back
 *   through the session file API. The marker is uploaded rather than written by
 *   the model, so the durability assertion cannot fail because a model declined
 *   to call a tool.
 *
 * Different-box identity is asserted directly because retaining the old id
 * would mean the product still paid for an OCI rootfs snapshot instead of using
 * the provider that owns workspace durability.
 *
 * This state-mutating spec creates and removes an assistant, two successive
 * workspace boxes and one Session, so it is exclusive and must stay in the
 * suite contract's one-worker serial group.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { parseTimeoutEnv } from '../fixtures/env';

// Two cold workspace provisions, a small file sync, and two model turns need a
// wider reply wait than one warm conversation, while the test itself stays
// inside the suite's fixed 180-second watchdog.
const REPLY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_ASSISTANT_HIBERNATE_REPLY_TIMEOUT_MS', 240_000);

// The session exposes the Assistant's durable workspace at `/workspace`; the
// backing profile path stays private to the runtime. So the marker goes at the
// visible root: naming another `workspace/` directory would append a second one.
const MARKER_DIR = '';
const MARKER_FILE = 'astrabox-park-marker.txt';

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered.
//
// Deleting the Assistant releases the workspace box, so it waits for the
// passing path like everything else. A failed run retains its resources for
// diagnosis; the campaign cleanup reconciles them afterwards.
const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('assistant workspace profile survives hibernate and wake on a fresh box', async ({ request }) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const marker = `astrabox-park-marker-${runId}`;

  // The environment must run the resident `assistant` engine. Borrowing the
  // seeded agent's environment does not work and is not a near miss: the engine
  // belongs to the environment, and `POST /assistants` rejects `claude_code`
  // with ASSISTANT_ENGINE_UNSUPPORTED before anything else happens.
  const environmentName = await api.assistantEnvironmentName();
  expect(environmentName, 'an assistant environment must exist').not.toEqual('');
  const assistantModel = await api.assistantModelName(environmentName);

  try {
    const assistant = await api.createAssistant({
      display_name: `__e2e_assistant_park_${runId}`,
      environment_name: environmentName,
      model_config_override: { model_name: assistantModel },
    });
    assistantId = String(assistant.assistant_id || '');
    expect(assistantId, 'created assistant must have an id').not.toEqual('');

    // ── 1. Wake: the workspace provisions its current box. ──────────────────
    const firstReady = await api.waitForWorkspaceReady(assistantId);
    const originalSandboxId = String(firstReady.current_sandbox_id || '').trim();
    expect(originalSandboxId, 'a READY workspace must name its sandbox').not.toEqual('');
    test.info().annotations.push({ type: 'e2e_workspace_sandbox_id', description: originalSandboxId });

    // ── 2. Converse, and leave a marker behind. ──────────────────────────────
    const firstSession = await api.startAssistantConversation(assistantId);
    const firstSessionId = String(firstSession.session_id || '');
    expect(firstSessionId, 'starting a conversation must open a session').not.toEqual('');
    sessions.push(firstSessionId);
    const firstSessionReady = await api.waitForSessionReady(firstSessionId);
    expect(firstSessionReady.terminal_cwd, 'Hermes must expose the common workspace root').toEqual(
      '/workspace',
    );

    const firstTurn = await api.sendTurn(firstSessionId, '请简短回复一句话，不要使用工具。', REPLY_TIMEOUT_MS);
    expect(firstTurn.errorText, 'the first turn must not error').toBeNull();
    expect(firstTurn.text.trim(), 'the first turn must produce a reply').not.toEqual('');

    await api.uploadFileText(firstSessionId, MARKER_DIR, MARKER_FILE, marker);
    const beforeHibernate = await api.downloadFileText(firstSessionId, MARKER_FILE);
    // Read it back BEFORE hibernating. Without this the spec cannot tell "hibernate
    // lost the file" from "the upload never landed", and would report the wrong
    // defect.
    expect(beforeHibernate, 'the marker must be readable before hibernate').toContain(marker);

    // ── 3. Hibernate: commit the workspace, then release the box. ────────────
    const hibernated = await api.hibernateWorkspace(assistantId);
    expect(hibernated.hibernated, 'hibernate must durably commit the workspace').toBe(true);
    expect(hibernated.released, 'hibernate must release the old box after the commit').toBe(true);
    expect(String(hibernated.previous_sandbox_id || '')).toEqual(originalSandboxId);
    expect(
      hibernated.sandbox_id,
      'a released box must not remain the workspace pointer',
    ).toBeNull();

    // ── 4. Wake again: another box receives the same workspace. ──────────────
    const secondReady = await api.waitForWorkspaceReady(assistantId);
    expect(
      String(secondReady.current_sandbox_id || ''),
      'wake must materialize a fresh box instead of rootfs-resuming the old one',
    ).not.toEqual(originalSandboxId);

    // ── 5. The original engine conversation resumes, and its files remain. ──
    // A new Session would prove only the visible workspace: Hermes can create a
    // blank conversation without the profile's state.db. Reusing this Session
    // makes the adapter resume its persisted native conversation key, so a pass
    // requires both the visible file tree and Hermes-owned profile state.
    const resumedSessionReady = await api.waitForSessionReady(firstSessionId);
    expect(
      resumedSessionReady.terminal_cwd,
      'a restored Hermes workspace must keep the common workspace root',
    ).toEqual('/workspace');

    const secondTurn = await api.sendTurn(
      firstSessionId,
      '请继续简短回复一句话，不要使用工具。',
      REPLY_TIMEOUT_MS,
    );
    expect(secondTurn.errorText, 'the turn after a wake must not error').toBeNull();
    expect(secondTurn.text.trim(), 'the turn after a wake must produce a reply').not.toEqual('');

    const afterWake = await api.downloadFileText(firstSessionId, MARKER_FILE);
    expect(
      afterWake,
      'the marker written before hibernate must still be in the workspace — this is ' +
        'the durable half of "durable personal workspace"',
    ).toContain(marker);
  } finally {
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
    // Deleting the assistant releases any workspace box created by this run.
  }
});
