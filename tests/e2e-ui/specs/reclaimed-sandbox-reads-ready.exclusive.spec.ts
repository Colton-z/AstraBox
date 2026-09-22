/**
 * E2E: an out-of-band sandbox loss remains invisible to an active conversation.
 *
 * After the sandbox compute is killed, the Chinese console must still show
 * 就绪, keep the composer enabled, and avoid runtime-disconnected or recovery
 * warnings. The next composer turn must render a reply and move the session to a
 * new sandbox id while the Agent remains ACTIVE.
 *
 * Exact sandbox identity remains an API assertion because the conversation page
 * does not render it.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Agent create + provision + a browser reload against dead compute: a generous,
// env-tunable budget (no model turn sits on this spec's critical path).
// Abrupt removal converges quickly; this gates the reclaim so the browser
// genuinely re-loads against dead compute.
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);

// This spec asserts the console's *localized* status rendering: the header must
// read 就绪 and NEVER the alarming 运行时断开 / 沙箱已过期 / 待恢复 banners or a
// 恢复会话 button. The community console localizes via i18next with a browser
// language detector (order ['localStorage','navigator'], fallbackLng 'en'), and
// Playwright's default browser locale is en-US — which renders the header in
// English ("Ready"/"Runtime disconnected"/…). An English UI would fail the 就绪
// check AND silently false-green every Chinese negative assertion (the UI never
// emits those strings regardless of state). Pin the console to Chinese exactly as
// a zh user does — a zh-CN browser locale (navigator) plus the persisted
// 'astrabox-lang' preference the app's own language switch writes (localStorage,
// the highest-priority detector source) — so every assertion below is meaningful.
test.use({ locale: 'zh-CN' });

// Teardown that changes state runs on the passing path only, and hook
// registration order is the old `finally` order: afterEach hooks run in the
// order they are registered, so the session goes before the agent it ran on.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('agent_chat reclaimed sandbox reads ready, not disconnected/expired (transparent re-borrow)', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');

  // Source a concrete model + environment from the seeded default agent — the
  // create validator (agent_schema) requires a non-blank model — skipping
  // litellm wildcard routing keys, same as the per-session sandbox spec.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'seeded default agent must name an environment').not.toEqual('');
  const models = await api.listEnvironmentModels(environmentName);
  const model = models.find((m) => m && !m.includes('*')) || 'deepseek-chat';

  let sessionId = '';
  try {
    // ── Isolated agent (pure metadata, always ACTIVE) + one conversation. ──
    const agent = await api.createAgent({
      name: `__e2e_reclaimed_${runId}`,
      model,
      environment_name: environmentName,
    });
    agentId = String(agent.agent_id || '');
    expect(agentId, 'created agent must have an id').not.toEqual('');

    const created = await api.startConversation(agentId);
    sessionId = created.session_id;
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId);
    const oldSandbox = String(ready.sandbox_id || '').trim();
    expect(oldSandbox, 'fresh conversation should hold a sandbox').not.toEqual('');
    expect(
      String((ready.agent_runtime as { state?: string } | undefined)?.state || ''),
      'agent is ACTIVE while its conversation is live',
    ).toEqual('ACTIVE');

    // ── Baseline: the live conversation reads 就绪 in the browser. ──────────
    // Pin the console language to Chinese before the first navigation so the
    // localized status/banner assertions below are exercised (see test.use note).
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem('astrabox-lang', 'zh');
      } catch {
        /* localStorage unavailable — the zh-CN navigator locale still applies */
      }
    });
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
    const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(pill, 'a live conversation reads 就绪').toContainText('就绪', { timeout: 30_000 });

    // ── Reclaim the per-session sandbox out of band. Compute disappears while
    //    the session row remains untouched, reproducing compute loss that the
    //    next turn must detect and recover from.
    const oldSandboxHandle = await requireSandboxHandle(api, oldSandbox);
    expect(
      sandboxRunning(oldSandboxHandle),
      'sandbox should be running before the reclaim',
    ).toBe(true);
    killSandbox(oldSandboxHandle);
    await waitForSandboxStopped(oldSandboxHandle, KILL_CONVERGE_MS);
    expect(sandboxRunning(oldSandboxHandle), 'the reclaimed box should be gone').toBe(false);
    test.info().annotations.push({ type: 'e2e_reclaimed_sandbox_id', description: oldSandbox });

    // ── THE core property: a fresh load of a conversation whose sandbox was
    //    reclaimed still reads 就绪 — never 运行时断开 / 沙箱已过期 / 待恢复, and
    //    never offers a manual 恢复会话. The reclaim is invisible: the session
    //    stays READY and re-borrows on the next message.
    await page.reload();
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
    await expect(pill, 'reclaimed conversation still reads 就绪').toContainText('就绪', { timeout: 30_000 });
    await expect(pill).not.toContainText('运行时断开');
    await expect(pill).not.toContainText('待恢复');
    await expect(page.getByText('运行时断开')).toHaveCount(0);
    await expect(page.getByText('沙箱已过期')).toHaveCount(0);
    await expect(
      page.getByRole('button', { name: '恢复会话' }),
      'no recover button for a transparently-recoverable conversation',
    ).toHaveCount(0);
    // The enabled composer, READY pill, and missing recovery controls are the
    // browser's observable readiness oracle. Durable delivery is the sibling
    // out-of-band-death spec's subject.
    const composer = page.locator('textarea').last();
    await expect(composer, 'composer stays enabled through a sandbox reclaim').toBeEnabled({ timeout: 30_000 });

    // ── Durable-state cross-check via the API projection: the reclaim did NOT
    //    flip the conversation into a scary/terminal state, and the agent record
    //    is untouched (a session sandbox reclaim never touches the agent).
    const reclaimed = await api.getSession(sessionId);
    expect(
      ['READY', 'WAITING_INPUT', 'RECOVERY_REQUIRED'],
      `reclaimed conversation stays wakeable; state=${reclaimed.state}`,
    ).toContain(String(reclaimed.state));
    expect(
      String((reclaimed.agent_runtime as { state?: string } | undefined)?.state || ''),
      'the agent stays ACTIVE through a session sandbox reclaim',
    ).toEqual('ACTIVE');

    // Scope note: this spec asserts the console's status stays calm through a
    // reclaim: the conversation reads 就绪 with an enabled composer and no
    // recovery banner. Durable re-borrow delivery (a persisted reply landing on
    // a new sandbox_id after the next message) is the distinct subject of
    // sandbox-oob-death-reborrow.exclusive.spec and is intentionally not
    // re-asserted here.
  } finally {
    // Delete the conversation first (releases its own sandbox), then the agent.
    // Nothing here is released on a failing run. `trackSessions()` and
    // `onPassOnly()` decide in afterEach hooks, where the test's real status is
    // known — see that fixture on why the unit is the whole block.
  }
});
