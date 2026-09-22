/**
 * E2E: a foreground turn sent while a background Agent is still running keeps
 * both assistant replies as separate messages.
 *
 * The foreground Bash turn must drive the session to PROCESSING while the
 * background task remains open. After both finish, the root messages remain
 * separate and contain no child lifecycle blocks; the independent Session
 * child-run projection carries the completed Agent and its transcript.
 *
 * Launching a background Agent is model-dependent. A bounded probe skips only
 * when no background state or child-run record appears.
 */
import { test, expect } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, messageText, type ChildRunRecord, type MessageRecord, type SessionRecord } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { nativeRootEntries, object } from '../fixtures/nativeMcpServer';
import { documentsByField } from '../fixtures/dbOracle';

// The two Bash turns rendezvous through a sandbox-local file. The background
// turn cannot settle before the foreground turn is active, and the foreground
// releases it without spending most of the suite's fixed 180-second budget on
// sleeps. Bounded probe for the first sign of a background subagent (OPEN
// background_task_state or any lifecycle frame) before skipping.
const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_PROBE_TIMEOUT_MS', 90_000);
// Foreground-idle wait and the concurrent foreground reply wait.
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
// How long the session may take to settle the background subagent (state READY,
// cleared background_task_state, one completed lifecycle) — the 90s sleep and
// the overlapping foreground turn both eat into this.
const BACKGROUND_SETTLED_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_SETTLED_TIMEOUT_MS', 240_000);
// How long to wait for the overlap turn to become the active (PROCESSING) turn.
const ACTIVE_TURN_POLL_MS = parseTimeoutEnv('ASTRABOX_E2E_ACTIVE_TURN_POLL_MS', 60_000);

// The foreground turn stays active long enough for the PROCESSING observation,
// then releases the already-running background Bash command.
const FOREGROUND_RELEASE_DELAY_SECONDS = 8;
const FOREGROUND_AFTER_RELEASE_SECONDS = 3;
const BACKGROUND_RELEASE_TIMEOUT_SECONDS = 120;

const LABEL = 'overlap';
// Foreground-idle includes READY with an open background task.
const FOREGROUND_IDLE_STATES = ['READY', 'BACKGROUND_RUNNING'] as const;
// PROCESSING is the rendered active-turn state. BUSY is accepted defensively
// because it can appear in stored session state during the same transition.
const ACTIVE_TURN_STATE = /^(PROCESSING|BUSY)$/;
const TERMINAL_STATES = new Set(['RECOVERY_REQUIRED', 'TERMINATED', 'DELETED', 'FAILED']);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Engine/UI identity is independent from the platform turn lifecycle. */
function messageIdentity(message: MessageRecord): string {
  const messageId = String(message.message_id || '').trim();
  if (!messageId) throw new Error('messages API returned a row without message_id');
  return messageId;
}

// Materialization must not duplicate persisted assistant text blocks.
function expectNoDuplicateTextBlocks(message: MessageRecord, label: string): void {
  const textBlocks = (message.blocks || [])
    .filter((block) => String(block.type || '') === 'text')
    .map((block) => String(block.text || block.content || '').trim())
    .filter((text) => text.length > 20);
  const duplicates = textBlocks.filter((text, index) => textBlocks.indexOf(text) !== index);
  expect(duplicates, `${label} should not duplicate persisted assistant text blocks`).toEqual([]);
}

// ── Prompts ────────────────────────────────────────────────────────────────
function backgroundAgentPrompt(options: { runId: number | string; label: string; releasePath: string }): string {
  const marker = `BG_${options.label}_${options.runId}_1`;
  return [
    `E2E background subagent ${options.label} ${options.runId}.`,
    'This is a product regression test. Follow the tool instructions literally.',
    'Task 1: launch one Agent tool call with run_in_background=true.',
    "The background Agent task must use Bash to run exactly: python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `release = Path(${JSON.stringify(options.releasePath)})`,
    `deadline = time.monotonic() + ${BACKGROUND_RELEASE_TIMEOUT_SECONDS}`,
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('foreground overlap release did not arrive')",
    '    time.sleep(0.2)',
    'release.unlink(missing_ok=True)',
    `print("${marker}_DONE")`,
    'PY',
    `After the Bash command completes, the background Agent final summary must contain ${marker}_DONE.`,
    'In the parent turn, do not wait for the background Agent to finish.',
    // "Containing X" is not enough: asked for one short sentence containing
    // PARENT_LAUNCHED_overlap_<runId>, deepseek-v4-flash answered "Parent
    // launched background overlap agent <runId>; awaiting its completion
    // notification" — it read the token as a description and wrote the
    // description. The marker is what identifies the launcher message among the
    // session's assistant messages, so it has to survive verbatim. This is the
    // same defence the Bash instruction above already carries.
    `Immediately after launching the background Agent call, reply with one short sentence that contains the token PARENT_LAUNCHED_${options.label}_${options.runId} copied verbatim.`,
    'Copy that token character for character. Do not rewrite it as prose, do not translate it, and do not replace its underscores with spaces.',
    'The parent turn must not use Bash directly.',
  ].join('\n');
}

function overlapReleasePath(runId: number | string): string {
  // Claude Bash and the foreground turn share the conversation workspace, not
  // their independently isolated, root-owned /tmp mounts.
  return `/workspace/.astrabox-e2e-overlap-${runId}.release`;
}

function backgroundDoneMarker(label: string, runId: number | string, index = 1): string {
  return `BG_${label}_${runId}_${index}_DONE`;
}

/** The foreground overlap turn: a REAL Bash turn that occupies the session. */
function foregroundOverlapPrompt(runId: number | string, marker: string, releasePath: string): string {
  return [
    `E2E foreground overlap ${runId}:`,
    'You MUST call the Bash tool to run the command below verbatim. Do not rewrite it as a plain-text description.',
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `time.sleep(${FOREGROUND_RELEASE_DELAY_SECONDS})`,
    `Path(${JSON.stringify(releasePath)}).touch()`,
    `time.sleep(${FOREGROUND_AFTER_RELEASE_SECONDS})`,
    `print("${marker}")`,
    'PY',
    'After the command finishes, reply with one short sentence.',
  ].join('\n');
}

// ── Page helpers ───────────────────────────────────────────────────────────
async function openSessionView(page: Page, sessionId: string): Promise<void> {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
}

// Filling avoids key events that could submit a multiline prompt early. Once
// accepted, the prompt is either still in the platform queue or has crossed the
// engine-consumption boundary into the transcript.
async function sendPromptFromComposer(page: Page, prompt: string, echoSubstring: string): Promise<void> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
  await composer.fill(prompt);
  const submit = page.getByTestId('composer-submit');
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: echoSubstring });
  const consumedPrompt = page.getByTestId('user-message').filter({ hasText: echoSubstring }).last();
  await expect(
    queuedPrompt.or(consumedPrompt).first(),
    'the prompt should appear in the platform queue or the consumed transcript',
  ).toBeVisible({ timeout: 30_000 });
}

async function closePageAfterAssertions(page: Page): Promise<void> {
  await page.goto('about:blank', { waitUntil: 'domcontentloaded', timeout: 5_000 }).catch(() => undefined);
  await page.close({ runBeforeUnload: false }).catch(() => undefined);
}

async function showAgentsTab(page: Page): Promise<void> {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible({ timeout: 15_000 });
}

function subagentRows(page: Page) {
  return page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row');
}

async function expectAgentsPanelRows(page: Page, minCount: number, label: string): Promise<void> {
  await expect
    .poll(async () => subagentRows(page).count(), {
      timeout: 45_000,
      message: `${label} should render subagent rows in the Agents panel`,
    })
    .toBeGreaterThanOrEqual(minCount);
  await expect(
    page.getByTestId('subagent-agents-panel').getByTestId('empty-state'),
  ).toHaveCount(0);
}

// A running row must open a transcript surface rather than an empty drawer.
async function expectLiveSubagentDrawer(page: Page, childRunId: string, label: string): Promise<Locator> {
  // The public child resource is the lifecycle authority. Its visible status is
  // engine-authored, so translated platform words cannot identify an open row.
  const row = page.locator(
    `[data-testid="subagent-agent-row"][data-child-run-id="${childRunId}"]`,
  );
  await expect(row, `${label} should render a live subagent row`).toBeVisible({ timeout: 45_000 });
  await row.click();
  const drawer = page.getByTestId('subagent-transcript-drawer');
  await expect(drawer, `${label} should open the subagent transcript drawer`).toBeVisible({ timeout: 15_000 });
  // Live surface = the waiting placeholder OR any real streamed content. The
  // content alternatives cover model-phrasing variance: the tool call itself
  // (Bash/python3), the marker (BG_), or the subagent's thinking about the
  // scripted task (mentions python/sleep in any casing).
  await expect(
    drawer.getByText(/Waiting for subagent output|Bash|python3?|BG_|sleep/i).first(),
    `${label} should render the live subagent transcript surface`,
  ).toBeVisible({ timeout: 15_000 });
  const tool = drawer.getByRole('button', { name: /^Bash / });
  await expect(tool).toHaveCount(1);
  await expect(tool).toHaveAttribute('aria-expanded', 'false');
  await tool.click();
  await expect(tool).toHaveAttribute('aria-expanded', 'true');
  return tool;
}

async function waitForOpenChildRun(
  api: AstraApi,
  sessionId: string,
  timeoutMs = 45_000,
): Promise<ChildRunRecord> {
  const deadline = Date.now() + timeoutMs;
  let last: ChildRunRecord[] = [];
  while (Date.now() < deadline) {
    last = (await api.listChildRuns(sessionId)).child_runs;
    const open = last.find((childRun) => !childRun.closed);
    if (open) return open;
    await sleep(1_000);
  }
  throw new Error(
    `Session ${sessionId} did not project an open child run within ${timeoutMs}ms; ` +
      `last=${JSON.stringify(last)}`,
  );
}

// A settled drawer shows one of: real transcript content, the no-transcript
// placeholder, or the load-error text. Anything else (the lifecycle-summary
// fallback rendered before the fetch resolves) is still in flight.
const DRAWER_TRANSCRIPT_CONTENT = /Bash|python3|Background task|Agent .* completed|BG_/;
const DRAWER_SETTLED =
  /Bash|python3|Background task|Agent .* completed|BG_|This subagent has no activity to show yet\./;

/**
 * The Agents panel reads the Session child-run index; the drawer fetches the
 * selected child's transcript on demand from
 * /sessions/{id}/child-runs/{childRunId}/messages. Its content arrives
 * one round trip AFTER the drawer becomes visible, and until then the drawer
 * renders the lifecycle summary — which is NOT the loading placeholder, since
 * `loading` is still false on first paint. So poll for a settled state rather
 * than for the placeholder to clear.
 *
 * Times out soft (returns whatever is on screen) so the caller's own assertion
 * reports the real diff instead of this helper masking it.
 */
async function settledDrawerText(
  drawer: Locator,
  page: Page,
  timeoutMs = 20_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let text = '';
  for (;;) {
    text = (await drawer.innerText()).replace(/\s+/g, ' ').trim();
    if (DRAWER_SETTLED.test(text) || Date.now() >= deadline) return text;
    await page.waitForTimeout(500);
  }
}

// Completed rows must expose real transcript output, not the empty placeholder.
async function expectSubagentDrawerOutput(page: Page, minCount: number): Promise<void> {
  const rows = subagentRows(page);
  const count = await rows.count();
  const inspected: Array<{ row: string; drawer: string }> = [];
  let meaningfulDrawerCount = 0;
  for (let index = 0; index < count; index += 1) {
    const row = rows.nth(index);
    const rowText = (await row.innerText()).replace(/\s+/g, ' ').trim();
    await row.click();
    const drawer = page.getByTestId('subagent-transcript-drawer');
    await expect(drawer, `subagent row ${index + 1} should open the transcript drawer`).toBeVisible({ timeout: 15_000 });
    const drawerText = await settledDrawerText(drawer, page);
    inspected.push({ row: rowText, drawer: drawerText });
    if (
      !drawerText.includes('This subagent has no activity to show yet.') &&
      DRAWER_TRANSCRIPT_CONTENT.test(drawerText)
    ) {
      meaningfulDrawerCount += 1;
    }
    await drawer.getByTestId('subagent-close-button').click();
    await expect(drawer).toHaveCount(0, { timeout: 10_000 });
  }
  expect(
    meaningfulDrawerCount,
    `the settled background Agent should expose a real transcript drawer; inspected=${JSON.stringify(inspected)}`,
  ).toBeGreaterThanOrEqual(minCount);
}

// ── Session waiters and launch probe ───────────────────────────────────────
async function waitForForegroundIdleOrBackground(
  api: AstraApi,
  sessionId: string,
  timeoutMs = TURN_TIMEOUT_MS,
): Promise<SessionRecord> {
  const deadline = Date.now() + timeoutMs;
  let last: SessionRecord | null = null;
  while (Date.now() < deadline) {
    last = await api.getSession(sessionId);
    const state = String(last.state || '');
    if (
      (FOREGROUND_IDLE_STATES as readonly string[]).includes(state) &&
      !last.current_turn_id &&
      !last.pending_interaction
    ) {
      return last;
    }
    if (TERMINAL_STATES.has(state)) {
      throw new Error(`Session ${sessionId} reached terminal ${state} while waiting for foreground-idle; last=${JSON.stringify(last)}`);
    }
    await sleep(1_500);
  }
  throw new Error(`Session ${sessionId} did not return to a foreground-idle state within ${timeoutMs}ms; last=${JSON.stringify(last)}`);
}

async function waitForAssistantTextMatching(
  api: AstraApi,
  sessionId: string,
  pattern: RegExp,
  timeoutMs: number,
): Promise<MessageRecord> {
  const deadline = Date.now() + timeoutMs;
  let lastAssistant = 0;
  while (Date.now() < deadline) {
    const page = await api.getMessages(sessionId, 50);
    const messages = page.messages || [];
    lastAssistant = messages.filter((m) => m.role === 'assistant').length;
    const hit = messages.find((m) => m.role === 'assistant' && pattern.test(messageText(m)));
    if (hit) return hit;
    await sleep(1_500);
  }
  throw new Error(`Session ${sessionId} did not receive assistant text matching ${pattern} within ${timeoutMs}ms (assistants=${lastAssistant})`);
}

async function waitForBackgroundSettledWithCompletedSubagents(
  api: AstraApi,
  sessionId: string,
  expectedCompletedCount: number,
  timeoutMs = BACKGROUND_SETTLED_TIMEOUT_MS,
): Promise<{ session: SessionRecord; messages: MessageRecord[]; childRuns: ChildRunRecord[] }> {
  const deadline = Date.now() + timeoutMs;
  let lastSession: SessionRecord | null = null;
  let lastChildRuns: ChildRunRecord[] = [];
  while (Date.now() < deadline) {
    lastSession = await api.getSession(sessionId);
    const page = await api.getMessages(sessionId, 50);
    lastChildRuns = (await api.listChildRuns(sessionId)).child_runs;
    const completed = lastChildRuns.filter((item) => item.closed);
    if (
      String(lastSession.state || '') === 'READY' &&
      !lastSession.background_task_state &&
      completed.length >= expectedCompletedCount
    ) {
      return { session: lastSession, messages: page.messages || [], childRuns: lastChildRuns };
    }
    if (TERMINAL_STATES.has(String(lastSession.state || ''))) {
      throw new Error(`Session ${sessionId} failed before background settled; last=${JSON.stringify(lastSession)}`);
    }
    await sleep(2_000);
  }
  throw new Error(
    `Session ${sessionId} did not settle background subagents within ${timeoutMs}ms; ` +
      `expected>=${expectedCompletedCount} completed, last_state=${lastSession?.state} ` +
      `last_child_runs=${JSON.stringify(lastChildRuns)}`,
  );
}

/**
 * Poll for the original launcher by its product-visible marker. Child-run
 * identity deliberately does not participate in root-message identity.
 */
async function captureBackgroundLauncher(
  api: AstraApi,
  sessionId: string,
  marker: string,
  timeoutMs = 20_000,
): Promise<MessageRecord> {
  const deadline = Date.now() + timeoutMs;
  let lastCount = 0;
  while (Date.now() < deadline) {
    const page = await api.getMessages(sessionId, 50);
    lastCount = (page.messages || []).filter((m) => m.role === 'assistant').length;
    const launcher = (page.messages || []).find(
      (m) => m.role === 'assistant' && messageText(m).includes(marker),
    );
    if (launcher) return launcher;
    await sleep(1_500);
  }
  throw new Error(
    `Session ${sessionId}: no assistant message carried ${JSON.stringify(marker)} within ${timeoutMs}ms ` +
      `(assistants=${lastCount}) — the parent turn did not persist its launcher reply`,
  );
}

/**
 * Require an open background manifest before starting the overlapping turn.
 * Its completion belongs to the child view, not to the launcher's speech.
 * A bounded idle grace allows the manifest projection to catch up.
 */
async function probeBackgroundTaskManifest(api: AstraApi, sessionId: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  const IDLE_NO_MANIFEST_GRACE_MS = 10_000;
  let idleWithLifecycleSince = 0;
  while (Date.now() < deadline) {
    const session = await api.getSession(sessionId);
    if (session.background_task_state) return true;
    if (TERMINAL_STATES.has(String(session.state || ''))) {
      throw new Error(`session ${sessionId} reached terminal state ${session.state} during background-manifest probe`);
    }
    const hasLifecycle = (await api.listChildRuns(sessionId)).child_runs.length > 0;
    const foregroundIdle =
      (FOREGROUND_IDLE_STATES as readonly string[]).includes(String(session.state || '')) &&
      !session.current_turn_id &&
      !session.pending_interaction;
    if (foregroundIdle && hasLifecycle) {
      if (idleWithLifecycleSince === 0) {
        idleWithLifecycleSince = Date.now();
      } else if (Date.now() - idleWithLifecycleSince >= IDLE_NO_MANIFEST_GRACE_MS) {
        return false;
      }
    } else {
      idleWithLifecycleSince = 0;
    }
    await sleep(1_500);
  }
  return false;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('foreground turn during a background Agent lifecycle preserves both assistant turns', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const releasePath = overlapReleasePath(runId);

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const transcriptPage = await page.context().newPage();

  try {
    await api.waitForSessionReady(sessionId);
    // The background Agent's Bash (and the foreground Bash) must run unattended,
    // so pin bypassPermissions — no tool-permission interaction may stall either
    // turn. Community conversations already default to this; make it explicit.
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    const beforeParentAssistantCount = await api.assistantCount(sessionId);

    // ── Launch the background subagent through the real composer. ─────────────
    await openSessionView(page, sessionId);
    const launchPrompt = backgroundAgentPrompt({ runId, label: LABEL, releasePath });
    await sendPromptFromComposer(
      page,
      launchPrompt,
      `PARENT_LAUNCHED_${LABEL}_${runId}`,
    );

    // Require the actual background manifest, not merely a launch response.
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. A declined ask re-sends the same prompt once, then fails.
    const launched = await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, launchPrompt);
      },
      probe: async () =>
        (await probeBackgroundTaskManifest(api, sessionId, PROBE_TIMEOUT_MS)) ? true : null,
      what: `No open background manifest was observed within ${PROBE_TIMEOUT_MS}ms; the overlap prerequisite is unproven`,
      budgetMs: PROBE_TIMEOUT_MS * 2,
      probeMs: PROBE_TIMEOUT_MS,
    });

    // ── The background lifecycle is VISIBLE and LIVE in the Agents panel. ─────
    const liveChild = await waitForOpenChildRun(api, sessionId);
    let liveToolId = '';
    await expect.poll(async () => {
      const transcript = await api.getChildRunMessages(sessionId, liveChild.child_run_id);
      const blocks = transcript.messages.flatMap((message) => message.content);
      const runningTool = blocks.find((block) => block.type === 'tool_use'
        && block.name === 'Bash' && JSON.stringify(block.input ?? null).includes(releasePath));
      liveToolId = String(runningTool?.id ?? '');
      return liveToolId !== '' && !blocks.some((block) => block.type === 'tool_result'
        && block.tool_use_id === liveToolId);
    }, { timeout: 45_000, message: 'the real child Bash must be waiting for the foreground release' }).toBe(true);

    // Keep a reader open while the original page submits the foreground turn.
    // Its expanded tool must survive both live updates and the durable snapshot.
    await openSessionView(transcriptPage, sessionId);
    await showAgentsTab(transcriptPage);
    await expectAgentsPanelRows(transcriptPage, 1, 'overlap background Agent');
    const expandedTool = await expectLiveSubagentDrawer(
      transcriptPage,
      liveChild.child_run_id,
      'overlap background Agent',
    );
    const originalToolNode = await expandedTool.elementHandle();
    expect(originalToolNode, 'the expanded tool has a mounted trigger').not.toBeNull();

    // Capture the launcher by its visible marker while the Session is
    // foreground-idle. Child lifecycle is Session-scoped and cannot identify
    // a root message.
    const backgroundOpen = await waitForForegroundIdleOrBackground(api, sessionId);
    expect(
      String(backgroundOpen.state || ''),
      `session should be foreground-idle before the overlap turn; detail=${JSON.stringify(backgroundOpen)}`,
    ).toMatch(/^(READY|BACKGROUND_RUNNING)$/);

    const parentMarker = `PARENT_LAUNCHED_${LABEL}_${runId}`;
    const parentLauncher = await captureBackgroundLauncher(api, sessionId, parentMarker);
    const backgroundChildRunId = liveChild.child_run_id;
    const parentLauncherId = messageIdentity(parentLauncher);
    expect(parentLauncherId, 'the launcher message should expose a stable identity (message_id / turn_id)').not.toEqual('');

    // ── CORE: dispatch a REAL Bash foreground turn WHILE the background task is
    //    still open. It must become the active (PROCESSING) turn — i.e. it truly
    //    occupies the session concurrently with the open background work. ───────
    const beforeForegroundAssistantCount = await api.assistantCount(sessionId);
    const foregroundMarker = `FOREGROUND_OVERLAP_${runId}_DONE`;
    await sendPromptFromComposer(
      page,
      foregroundOverlapPrompt(runId, foregroundMarker, releasePath),
      `E2E foreground overlap ${runId}`,
    );

    await expect
      .poll(async () => String((await api.getSession(sessionId)).state || ''), {
        timeout: ACTIVE_TURN_POLL_MS,
        message: 'the foreground overlap turn should become the active (PROCESSING) turn while the background task is still open',
      })
      .toMatch(ACTIVE_TURN_STATE);

    // ── The foreground turn produces its OWN assistant reply (its marker). ────
    const foregroundLive = await waitForAssistantTextMatching(api, sessionId, new RegExp(foregroundMarker), TURN_TIMEOUT_MS);
    expect(
      await api.assistantCount(sessionId),
      'the foreground overlap turn should add its own assistant message',
    ).toBeGreaterThan(beforeForegroundAssistantCount);
    expect(messageText(foregroundLive).trim(), 'foreground assistant response should not be empty').not.toEqual('');

    // ── Both turns settle: the background subagent completes and materializes. ─
    const settled = await waitForBackgroundSettledWithCompletedSubagents(api, sessionId, 1);
    const assistantMessages = settled.messages.filter((m) => m.role === 'assistant');

    const durableTranscript = await api.getChildRunMessages(sessionId, liveChild.child_run_id);
    const durableBlocks = durableTranscript.messages.flatMap((message) => message.content);
    expect(durableBlocks.filter((block) => block.type === 'tool_use' && block.id === liveToolId)).toHaveLength(1);
    const completedTool = durableBlocks.find((block) => block.type === 'tool_result'
      && block.tool_use_id === liveToolId);
    expect(completedTool, 'the same native tool must have its durable result').toBeTruthy();
    expect(completedTool?.is_error).not.toBe(true);
    expect(JSON.stringify(completedTool?.content)).toContain(backgroundDoneMarker(LABEL, runId));
    const retainedDrawer = transcriptPage.getByTestId('subagent-transcript-drawer');
    const durableText = durableBlocks.filter((block) => block.type === 'text'
      && String(block.text ?? '').trim());
    expect(String(durableText.at(-1)?.text ?? '')).toContain(backgroundDoneMarker(LABEL, runId));
    // Wait for the completed transcript, not merely a live tool result that
    // could have arrived before the final snapshot replaced it.
    const renderedText = retainedDrawer.locator('.markdown:not([data-testid="reasoning-part"] *)');
    await expect(renderedText).toHaveCount(durableText.length, { timeout: 45_000 });
    await expect(renderedText.last()).toContainText(backgroundDoneMarker(LABEL, runId));
    const resultHeading = retainedDrawer.getByRole('heading', { name: 'Result', exact: true });
    await expect(resultHeading).toBeVisible({ timeout: 45_000 });
    await expect(expandedTool).toHaveAttribute('aria-expanded', 'true');
    expect(
      await originalToolNode!.evaluate((node) => node.isConnected && node.getAttribute('aria-expanded') === 'true'),
      'materialization must preserve the original expanded tool, not replace and reopen it',
    ).toBe(true);
    await expect(resultHeading.locator('..')).toContainText(backgroundDoneMarker(LABEL, runId));

    // eslint-disable-next-line no-console
    console.error('DIAG parentLauncherId=' + JSON.stringify(parentLauncherId) +
      ' backgroundChildRunId=' + JSON.stringify(backgroundChildRunId) +
      ' foregroundMarker=' + JSON.stringify(foregroundMarker));
    for (const m of assistantMessages) {
      // eslint-disable-next-line no-console
      console.error('DIAG amsg identity=' + JSON.stringify(messageIdentity(m)) +
        ' message_id=' + JSON.stringify(m.message_id) + ' turn_id=' + JSON.stringify(m.turn_id) +
        ' child_blocks=' + JSON.stringify((m.blocks || []).filter((block) => block.type === 'subagent')) +
        ' text=' + JSON.stringify(messageText(m).slice(0, 120)));
    }
    // eslint-disable-next-line no-console
    console.error('DIAG settled.childRuns=' + JSON.stringify(settled.childRuns));
    const _diagLauncher = assistantMessages.find((m) => messageIdentity(m) === parentLauncherId);
    // eslint-disable-next-line no-console
    console.error('DIAG launcher.blocks=' + JSON.stringify((_diagLauncher?.blocks || []).map((b) => ({ type: b.type, data: b.data, id: b.id }))));

    // (1) BOTH assistant turns are preserved as separate messages.
    expect(
      assistantMessages.length,
      'the background-launch turn AND the foreground overlap turn should each persist as their own assistant message',
    ).toBeGreaterThanOrEqual(beforeParentAssistantCount + 2);

    // (2) The launcher retains its own speech; child output stays in its transcript.
    const backgroundAssistant = assistantMessages.find(
      (m) => messageIdentity(m) === parentLauncherId,
    );
    expect(
      backgroundAssistant,
      'the original launcher message should still be present after the overlap turn',
    ).toBeTruthy();
    const settledBackgroundChild = settled.childRuns.find(
      (childRun) => childRun.child_run_id === backgroundChildRunId,
    );
    expect(
      settledBackgroundChild,
      'the background child should close with Claude\'s exact completed status',
    ).toEqual(
      expect.objectContaining({
        closed: true,
        engine_kind: 'claude_code',
        engine_status: 'completed',
      }),
    );
    expect(backgroundAssistant!.content, 'child completion preserves the original launcher speech')
      .toBe(parentLauncher.content);
    expect((backgroundAssistant!.blocks || []).filter((block) => block.type === 'text'))
      .toEqual((parentLauncher.blocks || []).filter((block) => block.type === 'text'));
    const publicParentText = assistantMessages.flatMap((message) => message.blocks || [])
      .filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('\n');
    await expect.poll(() => nativeRootEntries(sessionId).flatMap(({ entry }) => {
      if (entry.type !== 'assistant' || entry.isSidechain === true) return [];
      const content = object(entry.message).content;
      return Array.isArray(content) ? content.map(object) : [];
    }).filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('\n'), {
      timeout: 30_000, message: 'public parent speech equals all actual native replies in order',
    }).toBe(publicParentText);
    expectNoDuplicateTextBlocks(backgroundAssistant!, 'materialized background launcher message');
    expect(
      (backgroundAssistant!.blocks || []).filter((block) => block.type === 'subagent'),
      'the launcher message must not hide Session child lifecycle blocks',
    ).toEqual([]);

    // (3) The foreground turn is a different message and also has no child
    //     lifecycle blocks.
    const foregroundAssistant = assistantMessages.find(
      (m) => messageText(m).includes(foregroundMarker),
    );
    expect(foregroundAssistant, 'the foreground overlap reply should persist as its own assistant message').toBeTruthy();
    expect(
      messageIdentity(foregroundAssistant!),
      'the foreground overlap turn must be a distinct message from the background launcher',
    ).not.toEqual(parentLauncherId);
    expect(
      (foregroundAssistant!.blocks || []).filter((block) => block.type === 'subagent'),
      'the foreground overlap message must not carry Session child lifecycle',
    ).toEqual([]);

    // ── UI: reload and confirm the completed background Agent materialized into
    //    the Agents panel (session events → registry rebuild), with a real drawer. ─
    const reconciledFrames = () => documentsByField('session_events', '$.session_id', sessionId)
      .filter((frame) => frame.source_kind === 'engine_child_reconcile');
    await api.listChildRuns(sessionId);
    const beforeReads = reconciledFrames();
    expect(beforeReads.length, 'live child history must have reached durable reconciliation').toBeGreaterThan(0);
    await Promise.all(Array.from({ length: 3 }, () => api.listChildRuns(sessionId)));
    const beforeSequences = new Set(beforeReads.map((frame) => frame.event_seq));
    const beforePayloads = new Set(beforeReads.map((frame) => JSON.stringify(frame.payload)));
    const repeatedFacts = reconciledFrames().filter((frame) =>
      !beforeSequences.has(frame.event_seq) && beforePayloads.has(JSON.stringify(frame.payload)));
    expect(repeatedFacts, 'concurrent child reads must not republish unchanged native history').toEqual([]);
    await openSessionView(page, sessionId);
    await showAgentsTab(page);
    await expectAgentsPanelRows(page, 1, 'settled background Agent');
    const settledStatus = settledBackgroundChild!.engine_status
      ?? settledBackgroundChild!.engine_reason
      ?? settledBackgroundChild!.engine_event;
    const settledRow = page.locator(
      `[data-testid="subagent-agent-row"][data-child-run-id="${backgroundChildRunId}"]`,
    );
    await expect(settledRow).toBeVisible({ timeout: 45_000 });
    await expect(
      settledRow.getByText(settledStatus, { exact: true }),
      'the settled background Agent should render its engine-authored status',
    ).toBeVisible();
    await expect(page.getByText('Running in background')).toHaveCount(0);
    await expectSubagentDrawerOutput(page, 1);
  } finally {
    await closePageAfterAssertions(transcriptPage);
    await closePageAfterAssertions(page);
    // The session is not deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});
