/** A held native child remains collectible beyond both scan and work-page limits. */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import {
  SEEDED_MANIFEST_TAG_PREFIX,
  newestOpenedManifestWindow,
  removeSeededBackgroundManifests,
  seedSettledBackgroundManifests,
  sessionEvents,
  type OpenedManifestWindowRow,
  type SeededBackgroundManifests,
} from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
test.use({ locale: 'en-US' });
const BACKGROUND_RUNNING_LABEL = 'Running in background';
const OPENED_EVENT_TYPE = 'turn.background_tasks_opened';
const MATERIALIZED_EVENT_TYPE = 'turn.background_tasks_materialized';
const ENGINE_MESSAGE_EVENT_TYPE = 'engine.message';
const BACKGROUND_WINDOW_LIMIT = 50;
const SEEDED_MANIFESTS = 55;

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
const OPENED_PROBE_MS = parseTimeoutEnv('ASTRABOX_E2E_BGWINDOW_OPENED_PROBE_MS', 60_000);
const CHILD_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_BGWINDOW_CHILD_SETTLE_MS', 45_000);
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_BGWINDOW_SETTLE_MS', 60_000);
const DB_POLL_INTERVALS = [1_000];

function backgroundLaunchPrompt(options: {
  marker: string;
  parentMarker: string;
  releasePath: string;
}): string {
  return [
    'Launch exactly one Agent with run_in_background=true.',
    'The child must use Bash to run this exact command:',
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `release = Path(${JSON.stringify(options.releasePath)})`,
    'deadline = time.monotonic() + 110',
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('background backlog fixture did not release the child')",
    '    time.sleep(0.2)',
    `print("${options.marker}")`,
    'PY',
    `The child must report ${options.marker} verbatim after the command completes.`,
    `Immediately after launching, reply only ${options.parentMarker} verbatim and end your turn.`,
    'Do not wait for the child and do not run Bash in the parent.',
  ].join('\n');
}

function eventType(event: Record<string, unknown>): string {
  return String(event.event_type || '').trim();
}

function eventPayload(event: Record<string, unknown>): Record<string, unknown> {
  const payload = event.payload;
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
}

function openedManifest(sessionId: string): Record<string, unknown> | null {
  return sessionEvents(sessionId).find((event) => eventType(event) === OPENED_EVENT_TYPE) ?? null;
}

function materializedFor(sessionId: string, openedSeq: number): Record<string, unknown> | null {
  return sessionEvents(sessionId).find((event) => (
    eventType(event) === MATERIALIZED_EVENT_TYPE
    && Number(eventPayload(event).source_opened_event_seq || 0) === openedSeq
  )) ?? null;
}

function nativeChildCompleted(payload: Record<string, unknown>, marker: string): boolean {
  const message = payload.message as Record<string, unknown> | undefined;
  const data = message?.data as Record<string, unknown> | undefined;
  return message?.subtype === 'task_notification'
    && ['TaskNotificationMessage', 'SystemMessage'].includes(String(message.__sdk_type))
    && data?.status === 'completed'
    && Boolean(data.task_id)
    && JSON.stringify(data).includes(marker);
}

function childEvidenceIsDurable(sessionId: string, marker: string): boolean {
  return sessionEvents(sessionId).some((event) => (
    eventType(event) === ENGINE_MESSAGE_EVENT_TYPE
    && nativeChildCompleted(eventPayload(event), marker)
  ));
}

interface HeaderClaim {
  pulse: string;
  claimsBackground: boolean;
}

async function headerClaim(page: Page): Promise<HeaderClaim> {
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  try {
    if (await pill.count() === 0) return { pulse: '<no header>', claimsBackground: false };
    const [pulse, label] = await Promise.all([pill.getAttribute('data-pulse'), pill.innerText()]);
    return {
      pulse: String(pulse ?? '<unset>'),
      claimsBackground: label.replace(/\s+/g, ' ').includes(BACKGROUND_RUNNING_LABEL),
    };
  } catch {
    return { pulse: '<reading>', claimsBackground: false };
  }
}
interface BackgroundWindowScene {
  sessionId: string;
  tag: string;
  openedSeq: number;
  marker: string;
  seeded: SeededBackgroundManifests[];
}

let scene: BackgroundWindowScene | null = null;

test.afterEach(async ({ request }, testInfo) => {
  const current = scene;
  scene = null;
  if (!current) return;
  try {
    if (testInfo.status !== testInfo.expectedStatus) {
      const api = new AstraApi(request);
      const [session, childRuns, messages] = await Promise.all([
        api.getSession(current.sessionId).catch((error: unknown) => ({ error: String(error) })),
        api.listChildRuns(current.sessionId).catch((error: unknown) => ({ error: String(error) })),
        api.getMessages(current.sessionId, 50).catch((error: unknown) => ({ error: String(error) })),
      ]);
      await testInfo.attach('background-window-scene.json', {
        contentType: 'application/json',
        body: JSON.stringify({
          session_id: current.sessionId,
          opened_event_seq: current.openedSeq,
          child_marker: current.marker,
          seed_tag: current.tag,
          seeded: current.seeded,
          session,
          child_runs: childRuns,
          messages,
          session_events: sessionEvents(current.sessionId),
          newest_opened_manifest_window: newestOpenedManifestWindow(BACKGROUND_WINDOW_LIMIT),
        }, null, 2),
      });
    }
  } finally {
    const removed = removeSeededBackgroundManifests({ prefix: current.tag });
    console.log(`background-window backlog removed: tag=${current.tag} rows=${removed}`);
  }
});
const sessions = trackSessions();

test('a background result reaches its conversation after fifty newer manifests were opened', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const marker = `BGWIN_${runId}_DONE`;
  const parentMarker = `PARENT_LAUNCHED_${runId}`;
  const tag = `${SEEDED_MANIFEST_TAG_PREFIX}${runId}`;
  const leftovers = removeSeededBackgroundManifests({ prefix: SEEDED_MANIFEST_TAG_PREFIX });
  if (leftovers > 0) {
    console.log(`background-window: swept ${leftovers} seeded row(s) left by an earlier run`);
  }
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const current: BackgroundWindowScene = { sessionId, tag, openedSeq: 0, marker, seeded: [] };
  scene = current;
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  await api.setPermissionMode(sessionId, 'bypassPermissions');

  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  await openSessionView(page, sessionId);
  const releaseName = `background-window-${runId}.release`;
  const prompt = backgroundLaunchPrompt({
    marker,
    parentMarker,
    releasePath: `/workspace/${releaseName}`,
  });
  await sendPrompt(page, sessionId, prompt);
  await expect.poll(() => openedManifest(sessionId), { timeout: OPENED_PROBE_MS }).not.toBeNull();
  const opened = openedManifest(sessionId)!;
  const openedSeq = Number(opened.event_seq || 0);
  const openedAt = String(opened.occurred_at || '');
  expect(openedSeq, 'the opened manifest must carry its journal sequence').toBeGreaterThan(0);
  expect(openedAt, 'the opened manifest must carry its timestamp').not.toEqual('');
  current.openedSeq = openedSeq;
  // Settled journal pairs compress unrelated history; the held child is real.
  const seeded = seedSettledBackgroundManifests({
    count: SEEDED_MANIFESTS,
    afterOccurredAt: openedAt,
    tag,
  });
  current.seeded.push(seeded);
  // Cross the 500-record scan bound as well as the 50-record work page.
  const overflow = seedSettledBackgroundManifests({
    count: 500, afterOccurredAt: seeded.lastOccurredAt, tag: `${tag}-overflow`,
  });
  current.seeded.push(overflow);
  expect(overflow.rows).toBe(1000);
  expect(seeded.rows, 'each seeded manifest is an opened row and its counterpart')
    .toBe(SEEDED_MANIFESTS * 2);
  expect(
    materializedFor(sessionId, openedSeq),
    'the child must remain held until every newer settled manifest is present',
  ).toBeNull();

  const openedWindow: OpenedManifestWindowRow[] = newestOpenedManifestWindow(BACKGROUND_WINDOW_LIMIT);
  expect(
    openedWindow.length,
    `fewer than ${BACKGROUND_WINDOW_LIMIT} opened manifests exist deployment-wide, so nothing `
      + 'was buried; the seeding did not land',
  ).toBe(BACKGROUND_WINDOW_LIMIT);
  expect(
    openedWindow.some((row) => row.session_id === sessionId),
    `this conversation's manifest is still inside the sweep's ${BACKGROUND_WINDOW_LIMIT}-row `
      + 'window, so the run proves nothing about a manifest that falls out of it; '
      + `window tail=${JSON.stringify(openedWindow.slice(-3))}`,
  ).toBe(false);
  await page.goto(appPath('/manage/sessions'), { waitUntil: 'domcontentloaded' });
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/sessions'));
  await expect(
    page.getByTestId('run-view'),
    'leaving the conversation must tear its view down, or the user never left',
  ).toHaveCount(0);
  const ceilingWindow = newestOpenedManifestWindow(500);
  expect(ceilingWindow).toHaveLength(500);
  expect(ceilingWindow.some((row) => row.session_id === sessionId)).toBe(false);
  await api.uploadFileText(sessionId, '/workspace', releaseName, 'release', 10_000);

  await expect
    .poll(() => childEvidenceIsDurable(sessionId, marker), {
      timeout: CHILD_SETTLE_MS,
      intervals: DB_POLL_INTERVALS,
      message:
        `no durable ${ENGINE_MESSAGE_EVENT_TYPE} for ${sessionId} carries ${marker} within `
        + `${CHILD_SETTLE_MS}ms. The background child never settled, so this run did not `
        + 'exercise a starved window and a red below would be misattributed to the platform.',
    })
    .toBe(true);
  await openSessionView(page, sessionId);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
  await expect
    .poll(async () => {
      const session = await api.getSession(sessionId);
      const backgroundTaskState = session.background_task_state;
      const header = await headerClaim(page);
      return {
        result_materialized: materializedFor(sessionId, openedSeq) !== null,
        session_state: String(session.state || ''),
        background_task_open: Boolean(
          backgroundTaskState
          && typeof backgroundTaskState === 'object'
          && String((backgroundTaskState as Record<string, unknown>).state || '') === 'OPEN',
        ),
        header_pulse: header.pulse,
        header_claims_background: header.claimsBackground,
      };
    }, {
      timeout: SETTLE_MS,
      intervals: DB_POLL_INTERVALS,
      message:
        'a background result must reach its conversation even when fifty newer manifests were '
        + 'opened while it waited, even beyond a complete bounded scan pass.',
    })
    .toEqual({
      result_materialized: true,
      session_state: 'READY',
      background_task_open: false,
      header_pulse: 'false',
      header_claims_background: false,
    });
  await expect(
    page.getByTestId('assistant-message')
      .filter({ hasText: /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i }),
    'no error bubble may stand in for the background result',
  ).toHaveCount(0);
});
