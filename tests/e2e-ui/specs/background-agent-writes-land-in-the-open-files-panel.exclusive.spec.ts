/**
 * E2E: a file a background Agent writes reaches the Files panel the user is
 * already looking at, without the user asking for it.
 *
 * The journey is a user who launches a background Agent and then sits on the
 * Files tab. They never press the panel's refresh button, never re-open a
 * directory, never switch tabs and never reload. When the child run finishes,
 * the file it wrote must be a row in the tree.
 *
 * WHAT MAKES THIS SPEC MEAN ANYTHING — read before editing. Every one of these
 * acts heals the thing under test, and any of them turns the spec into a
 * tautology that passes forever:
 *   - switching tabs: `TabsContent` is a Base UI `Tabs.Panel`, whose
 *     `keepMounted` defaults to false, so leaving and returning REMOUNTS
 *     `SessionFilesPanel` and its mount load refetches the root. That includes
 *     the Agents tab, which is why the child is watched through the API and
 *     through the Agents tab's label, never by opening it;
 *   - the panel's own refresh button and `page.reload()`: both refetch by hand,
 *     which is exactly what the user in this journey did not do;
 *   - expanding a directory: `refresh({force:true})` in `useSessionFiles` only
 *     refetches the directories it has loaded, so the child must write into a
 *     directory the panel already has open. That is why the marker lands in
 *     the workspace ROOT, which the panel's mount load opens.
 * The app-wide MANUAL_REFRESH_EVENT is not a control either: its listeners in
 * `useSessionRefreshEffects` (rehydrate) and `useSessionBootstrapEffects`
 * (lifecycle refresh) do not touch the file tree.
 *
 * HOW THE PANEL LEARNS. `useSessionFiles` has no interval and no feed; outside
 * its mount load the only refetch input is `refreshSignal`, the nonce
 * `useSessionRefreshEffects` bumps on the working true→false edge.
 * `SessionPage` passes it `isWorking || isSending`, and `useSessionChat`'s
 * `isWorking` is a response this page's stream is writing, or the session's
 * own PROCESSING state.
 *
 * WHICH EDGE THIS RUN EXERCISES, stated plainly because it bounds what a green
 * means. A Claude Code background Agent works after the reply that launched it
 * has ended, so its write lands outside any response. When it finishes, the CLI
 * queues a `<task-notification>` and Claude answers it on its own: the session's
 * reader records a response of origin `engine` that answers no input
 * (`engine/claude_runtime_client.py`, the runner's `engine_boundary`). That
 * response's end is the edge after the write, so a green here is the
 * user-facing outcome on the shape where the runtime answers a finished child,
 * not a differential on the edge itself. What keeps a red legible is the
 * MECHANISM assertion, which separates "the panel never asked the server again"
 * from "it asked and still did not render the row", plus the annotated request
 * timeline saying when each ask happened.
 *
 * The user sees the child running from the tab strip: `getRightPanelTabLabel`
 * labels the tab `Agents (N)` while N children read as active by the engine's
 * own status, and plain `Agents` once none is.
 *
 * ENGINE: Claude Code. `run_in_background` on the Agent tool, the
 * `async_launched` receipt and the `<task-notification>` queueing are Claude
 * Code's own vocabulary (`engine/claude_code_background.py`). The requirement
 * is asserted on the child-run row (engine_kind) rather than left to a probe
 * timeout, so a mis-selected profile fails at a named assertion. The prompt
 * below is written in Claude Code's tool names and only that.
 */
import { test, expect } from '@playwright/test';
import type { Response } from '@playwright/test';

import { AstraApi, type ChildRunRecord } from '../fixtures/astraApi';
import { nativeTranscriptRows, sessionResponseRows } from '../fixtures/dbOracle';
import { PlatformApi } from '../fixtures/platformApi';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { insist } from '../fixtures/insist';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// The spec reads three localized surfaces — the "Files" tab label
// (chat:panel.tab.files), "Current directory" (chat:files.current_directory)
// and "Upload" (chat:files.upload). The console detects language as
// ['localStorage','navigator'], so an unpinned runner locale decides which
// spelling it gets, and a spec that accepted two spellings would also accept a
// third nobody wrote it against. The Agents tab label is built in code and is
// the same in both locales.
test.use({ locale: 'en-US' });

// A conversation on the prewarmed lane claims its box in about a second; the
// rest of the 180s wall belongs to the reply, the child and the settle.
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
// The panel's own mount load: root listing + tree paint.
const PANEL_MOUNT_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_FILES_PANEL_MOUNT_TIMEOUT_MS', 30_000);
// First evidence that the model actually backgrounded an Agent. Deliberately
// tight: this journey still has a release, a child completion and the
// runtime's answer to it to pay for after the probe.
const BACKGROUND_PROBE_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_BACKGROUND_FILES_PROBE_TIMEOUT_MS',
  60_000,
);
// The child-run row appears at launch (async_launched), long before it closes.
const CHILD_ROW_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_CHILD_RUN_ROW_TIMEOUT_MS', 20_000);
// The background window: the launch reply has ended and the child holds its gate.
const BACKGROUND_WINDOW_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_BACKGROUND_WINDOW_TIMEOUT_MS',
  30_000,
);
// From releasing the gate to the file existing in the sandbox.
const WORKSPACE_WRITE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_WORKSPACE_WRITE_TIMEOUT_MS', 30_000);
// Child closes and the runtime's own answer to it ends.
const BACKGROUND_SETTLED_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_BACKGROUND_FILES_SETTLED_TIMEOUT_MS',
  45_000,
);
// The subject: how long the panel may take to show the row on its own.
const FILES_REFRESH_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_FILES_REFRESH_TIMEOUT_MS', 30_000);

// The child's own bound on the release gate. It raises rather than returning, so
// a test that dies before releasing leaves a failed child run instead of a
// silent success that proves nothing about ordering.
const CHILD_RELEASE_DEADLINE_SECONDS = 90;

// agent-engine-matrix.json: the claude_code profile's child_completion declares
// exactly this terminal status for a background Agent that finished its task.
const CLAUDE_CODE_CHILD_COMPLETED = 'completed';

// Claude Code keeps each Agent's conversation in a transcript of its own,
// `subagents/agent-<id>` in the session's SessionStore mirror
// (`engine/claude_code_background.py`).
const AGENT_TRANSCRIPT_PREFIX = 'subagents/agent-';

/** The input id the inputs route answered a composer send with. */
async function acceptedInputId(response: Response): Promise<string> {
  const body = (await response.json()) as { data?: { input_id?: unknown } };
  const inputId = String(body.data?.input_id ?? '').trim();
  expect(inputId, 'the inputs route must answer with the accepted input id').not.toBe('');
  return inputId;
}

function contentBlocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  return Array.isArray(content)
    ? content.filter((block): block is Record<string, unknown> => Boolean(block) && typeof block === 'object')
    : [];
}

interface ChildBashEvidence {
  input: Record<string, unknown>;
  /** The tool_result blocks Claude recorded for that call, in transcript order. */
  results: Record<string, unknown>[];
}

/**
 * The child's own Bash call for `commandFragment`, read from its transcript in
 * the SessionStore mirror — the supplier's record, independent of the
 * child-run projection the console reads.
 */
function childBash(sessionId: string, commandFragment: string): ChildBashEvidence[] {
  const rows = nativeTranscriptRows(sessionId)
    .filter((row) => String(row.subpath ?? '').startsWith(AGENT_TRANSCRIPT_PREFIX))
    .map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>);
  const blocks = rows.flatMap((entry) => contentBlocks(entry));
  const calls = new Map<string, Record<string, unknown>>();
  for (const block of blocks) {
    if (block.type !== 'tool_use' || block.name !== 'Bash') continue;
    if (!JSON.stringify(block.input ?? null).includes(commandFragment)) continue;
    calls.set(String(block.id ?? ''), (block.input ?? {}) as Record<string, unknown>);
  }
  return [...calls].filter(([id]) => id).map(([id, input]) => ({
    input,
    results: blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === id),
  }));
}

// Sessions are deleted only when the test passes; a failure keeps the scene and
// names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test("a background Agent's new file reaches the open Files panel without the user asking", async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = Date.now();
  const markerName = `bg-report-${runId}.md`;
  const markerContent = `BG_FILE_${runId}`;
  const wroteMarker = `BG_WROTE_${runId}`;
  const parentMarker = `PARENT_LAUNCHED_${runId}`;

  // ── Arrangement stays on the API: a user picks a conversation, they do not ─
  // author one to look at a file panel.
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  // Explicit, so the child's one Bash call runs unattended instead of resting on
  // whatever default the conversation was created with.
  await api.setPermissionMode(sessionId, 'bypassPermissions');

  // The panel's root comes from the product, never from a literal: `useSessionFiles`
  // applies the listing's `root_path`, and the short-cwd profile can move it.
  const rootListing = await platform.listFiles(sessionId);
  const root = String(rootListing.root_path || '').trim();
  expect(root, 'the files listing must name the workspace root the panel will open').not.toEqual('');
  const markerPath = `${root}/${markerName}`;
  const releaseName = `.e2e-release-${runId}`;
  const releasePath = `${root}/${releaseName}`;

  // ── The meter: every workspace listing the PAGE asks for ──────────────────
  // Exact pathname equality on the panel's own endpoint. Reads this spec makes
  // through the APIRequestContext do not go through the page and are not
  // counted, which is what lets the oracles below re-ask the platform without
  // disturbing the measurement.
  const fileListPath = apiPath(`/sessions/${sessionId}/files/list`);
  const fileListCalls: number[] = [];
  page.on('request', (outgoing) => {
    if (outgoing.method() !== 'POST') return;
    if (new URL(outgoing.url()).pathname === fileListPath) fileListCalls.push(Date.now());
  });

  // Pin the console language before the FIRST navigation, or the labels below
  // read whatever the runner's locale happens to be.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  await openSessionView(page, sessionId);

  // ── Sit down on the Files tab and let it finish loading ───────────────────
  // Files is already the default tab (sessionCapabilities.ts defaultTab), so the
  // click selects what is selected: it states the journey without remounting the
  // panel. After this point the spec performs NO right-panel interaction at all.
  await page.getByRole('tab', { name: 'Files', exact: true }).click();
  await expect(
    page.getByText('Current directory'),
    'the Files panel must be the one the user is sitting on',
  ).toBeVisible({ timeout: PANEL_MOUNT_TIMEOUT_MS });
  const tree = page.getByRole('tree');
  await expect(tree, 'the mount load must have produced a tree, not an empty state').toBeVisible({
    timeout: PANEL_MOUNT_TIMEOUT_MS,
  });
  // `busy` gates this button on the hook's own loading flag, so an enabled
  // Upload is the panel saying its root listing has landed.
  await expect(page.getByRole('button', { name: 'Upload', exact: true })).toBeEnabled({
    timeout: PANEL_MOUNT_TIMEOUT_MS,
  });

  // BASELINE. The tree is loaded and does not already contain the name the
  // final assertion looks for — so that assertion cannot be matching something
  // that was there all along.
  await expect(
    tree.getByText(markerName, { exact: true }),
    'the marker file must not exist before the background Agent writes it',
  ).toHaveCount(0);

  // ── Launch the background Agent through the real composer ─────────────────
  // The child blocks on a release file this test writes out of band. A fixed
  // sleep spends the budget without proving the ordering; the gate puts the
  // write strictly inside the background window.
  const childCommand = [
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `release = Path(${JSON.stringify(releasePath)})`,
    `deadline = time.monotonic() + ${CHILD_RELEASE_DEADLINE_SECONDS}`,
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    '        raise TimeoutError("the test never released the background Agent")',
    '    time.sleep(0.1)',
    `Path(${JSON.stringify(markerPath)}).write_text(${JSON.stringify(markerContent)})`,
    `print(${JSON.stringify(wroteMarker)})`,
    'PY',
  ].join('\n');
  const prompt = [
    `E2E background files ${runId}. This is a product regression test; follow the tool instructions literally.`,
    'Launch exactly one general-purpose Agent with run_in_background=true.',
    'Its complete task is: use Bash exactly once, with run_in_background=false, to run the following command, wait for it,',
    `then give a short completion report containing ${wroteMarker}.`,
    '---',
    childCommand,
    '---',
    'Do not use Bash yourself, do not launch a second Agent, and do not wait for this one.',
    `Immediately after the Agent call, reply with one short sentence containing ${parentMarker} and end your turn.`,
  ].join('\n');
  let launchInputId = await acceptedInputId(await sendPrompt(page, sessionId, prompt));

  // ── Did the model actually background an Agent? ───────────────────────────
  // Ask again rather than skip: a runtime test.skip() SIGTERMs the worker and
  // the lane counts the round exactly as a failure, so the model's choice would
  // decide the round instead of the platform's behaviour.
  await insist<true>({
    ask: async (attempt) => {
      if (attempt > 1) launchInputId = (await api.postTurnInput(sessionId, prompt)).input_id;
    },
    probe: async () => {
      const deadline = Date.now() + BACKGROUND_PROBE_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if ((await api.listChildRuns(sessionId)).child_runs.length > 0) return true;
        const state = String((await api.getSession(sessionId)).state || '');
        if (state === 'TERMINATED' || state === 'DELETED') {
          throw new Error(`session ${sessionId} reached terminal state ${state} during the probe`);
        }
        await page.waitForTimeout(1_500);
      }
      return null;
    },
    what:
      `no background Agent within ${BACKGROUND_PROBE_TIMEOUT_MS}ms (no child-run row): the model `
      + 'answered inline instead of launching a run_in_background Agent, and this spec\'s subject '
      + 'only exists once one is running.',
    budgetMs: BACKGROUND_PROBE_TIMEOUT_MS * 2,
    probeMs: BACKGROUND_PROBE_TIMEOUT_MS,
  });

  // ENGINE. Asserted on the row the moment one exists, so a profile this spec
  // cannot serve fails here by name rather than somewhere downstream.
  const launched = await api.waitForChildRuns(
    sessionId,
    (rows) => rows.length >= 1,
    CHILD_ROW_TIMEOUT_MS,
  );
  expect(
    launched[0]!.engine_kind,
    'this journey is written in Claude Code\'s background vocabulary '
      + '(run_in_background, async_launched, task-notification); another engine profile '
      + 'cannot serve it — select Claude Code for this lane',
  ).toBe('claude_code');

  // ── The background window, from the user's seat ───────────────────────────
  // The launch reply has ended: the page's working edge for it has already
  // fired, so the tree on screen holds no write of the child's yet.
  const launchReply = await api.waitForReplyTo(sessionId, launchInputId, {
    timeoutMs: BACKGROUND_WINDOW_TIMEOUT_MS,
  });
  expect(launchReply.metadata?.outcome, 'the reply that launched the child must complete').toBe('completed');
  // The child is at its gate in its own transcript: its Bash call is recorded
  // and has no result, so the write the panel must learn about is still ahead.
  await expect.poll(() => childBash(sessionId, releasePath).length, {
    timeout: BACKGROUND_WINDOW_TIMEOUT_MS,
    message: 'the background Agent must record its one gated Bash call in its own transcript',
  }).toBe(1);
  expect(
    childBash(sessionId, releasePath)[0]!.input.run_in_background,
    'the gated command must run in the child\'s foreground, so its result stays owed until release',
  ).not.toBe(true);
  expect(
    childBash(sessionId, releasePath)[0]!.results,
    'the gated Bash call must still be running — nothing has released it',
  ).toEqual([]);
  // What the user is shown while the child works: the tab strip counts it.
  const agentsTabWhileRunning = page.getByRole('tab', { name: 'Agents (1)', exact: true });
  await expect(
    agentsTabWhileRunning,
    'the Agents tab must count the child the user left running',
  ).toBeVisible({ timeout: BACKGROUND_WINDOW_TIMEOUT_MS });
  expect(
    (await api.getSession(sessionId)).current_responses ?? [],
    'the child works outside any response: nothing of the conversation\'s own is running',
  ).toEqual([]);

  // Quiesce the meter. A listing still in flight from the mount load, or from
  // the launch reply's end, would otherwise be counted as an after-the-write
  // refetch and the MECHANISM assertion would pass on the wrong evidence.
  const quiesceDeadline = Date.now() + BACKGROUND_WINDOW_TIMEOUT_MS;
  let previousCount = -1;
  while (fileListCalls.length !== previousCount) {
    if (Date.now() >= quiesceDeadline) {
      throw new Error(
        `the page never stopped listing the workspace: ${fileListCalls.length} POSTs to `
          + `${fileListPath} and still climbing, so no "after the write" boundary exists to measure`,
      );
    }
    previousCount = fileListCalls.length;
    await page.waitForTimeout(2_000);
  }
  const callsBeforeWrite = fileListCalls.length;
  await expect(
    tree.getByText(markerName, { exact: true }),
    'the tree on screen must not hold the marker before the child is released',
  ).toHaveCount(0);

  // ── Release the child, out of band ────────────────────────────────────────
  // Through the test's own request context, never through the page: the panel
  // must not learn anything from this act. The release file is written by the
  // test, not by the child, so nothing below ever asserts on it.
  const releasedAt = Date.now();
  await api.uploadFileText(sessionId, root, releaseName, 'release');

  // ── The workspace really has the file ─────────────────────────────────────
  // Asserted BEFORE the UI, so a red below can never be blamed on a model that
  // never wrote anything.
  await expect
    .poll(
      async () => {
        const listing = await platform.listFiles(sessionId, root, 15_000);
        return (listing.entries || []).some(
          (entry) => entry.name === markerName && entry.kind === 'file',
        );
      },
      {
        timeout: WORKSPACE_WRITE_TIMEOUT_MS,
        intervals: [500, 1_000],
        message: 'the background Agent must actually write the marker into the workspace root',
      },
    )
    .toBe(true);
  expect(
    await api.downloadFileText(sessionId, markerPath, 15_000),
    'the marker in the sandbox must be the one the child was told to write',
  ).toContain(markerContent);

  // ── Wait out the completion through the API only ──────────────────────────
  // Never the Agents tab: opening it unmounts the Files panel and its remount
  // would heal exactly the staleness under test.
  let closedChildren: ChildRunRecord[] = [];
  let childClosedAt = 0;
  try {
    closedChildren = await api.waitForChildRuns(
      sessionId,
      (rows) => rows.length >= 1 && rows.every((row) => row.closed),
      BACKGROUND_SETTLED_TIMEOUT_MS,
    );
    childClosedAt = Date.now();
    expect(
      closedChildren.every((row) => row.engine_status === CLAUDE_CODE_CHILD_COMPLETED),
      'a Claude Agent that finished its task retains the engine\'s own completed status; '
        + `rows=${JSON.stringify(closedChildren.map((row) => ({
          id: row.child_run_id, status: row.engine_status, reason: row.engine_reason,
        })))}`,
    ).toBe(true);
    const childResults = childBash(sessionId, releasePath)[0]?.results ?? [];
    expect(
      JSON.stringify(childResults),
      'the child\'s own transcript must hold its Bash result with the receipt it printed',
    ).toContain(wroteMarker);

    // The runtime answers the finished child on its own: one response that no
    // input started. Its end is the working edge the panel refetches on.
    await expect
      .poll(
        () => sessionResponseRows(sessionId)
          .filter((row) => row.origin === 'engine')
          .map((row) => ({ status: row.status, input_ids: row.input_ids })),
        {
          timeout: BACKGROUND_SETTLED_TIMEOUT_MS,
          intervals: [1_000, 2_000],
          message: 'Claude must answer the finished child with one response of its own that ends',
        },
      )
      .toEqual([{ status: 'ended', input_ids: [] }]);
    await expect
      .poll(
        async () => {
          const detail = await api.getSession(sessionId);
          return String(detail.state || '') === 'READY' && (detail.current_responses ?? []).length === 0;
        },
        {
          timeout: BACKGROUND_SETTLED_TIMEOUT_MS,
          intervals: [1_000, 2_000],
          message: 'the conversation must be at rest once the child and the runtime\'s answer to it are done',
        },
      )
      .toBe(true);
    await expect(
      page.getByRole('tab', { name: 'Agents', exact: true }),
      'the Agents tab must stop counting the child once it has closed',
    ).toBeVisible({ timeout: BACKGROUND_WINDOW_TIMEOUT_MS });

    // ── THE CLAIM ────────────────────────────────────────────────────────────
    // No manual refresh, no re-opened directory, no tab switch, no reload: the
    // file the background Agent wrote is a row in the tree the user is looking
    // at. Scoping to the tree is load-bearing — the prompt and the child's
    // completion report both echo this name into the transcript, so an unscoped
    // getByText would pass while the tree stayed empty.
    await expect(
      tree.getByText(markerName, { exact: true }),
      'the Files panel must show what the background Agent wrote, without being asked',
    ).toBeVisible({ timeout: FILES_REFRESH_TIMEOUT_MS });

    // MECHANISM. Separates "the panel never asked the server again" from "it
    // asked and still did not render the row" — the two reds have different
    // owners and the timeline annotation below says which one happened.
    expect(
      fileListCalls.filter((at) => at > releasedAt).length,
      'the panel must have asked the server for the workspace after the write landed',
    ).toBeGreaterThan(0);
  } finally {
    // The timeline is in the report whether this passed or failed: on a red it
    // is the whole diagnosis, and it survives the failure because it is pushed
    // here rather than after the assertions.
    test.info().annotations.push({
      type: 'files_list_timeline',
      description: [
        `root=${root} marker=${markerName}`,
        `POSTs before the release: ${callsBeforeWrite}`,
        `released_at=+0ms`,
        childClosedAt ? `child_closed=+${childClosedAt - releasedAt}ms` : 'child_closed=never',
        `files/list POSTs relative to the release: `
          + JSON.stringify(fileListCalls.map((at) => at - releasedAt)),
      ].join('; '),
    });
  }
});
