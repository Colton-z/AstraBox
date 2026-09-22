/**
 * E2E: the original conversation resumes on the same sandbox after its
 * image-baked runner is terminated and relaunched with empty process memory.
 *
 * The host-cache eviction specs inject a different fault: there the in-box
 * process keeps the SDK session in memory and accepts `attach`. Here the
 * runner process itself is gone. A warm turn plants a token as conversation
 * data and a marker file in the workspace; the spec then terminates the
 * runner and relaunches it through the image's own launcher, proving a
 * different PID for the same interpreter, command line, account, working
 * directory and runner file digest. The next message is the first thing to
 * touch the dead link: the fresh process refuses `attach`, the host configures
 * it with the engine's native resume, and the reply must carry the planted
 * token while the session stays on the same physical sandbox with no error.
 * The native SessionStore rows the platform holds for the conversation keep
 * their identity and payload and gain the resumed turn under the same native
 * session key.
 */
import { randomBytes } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import {
  IMAGE_RUNNER_LAUNCHER,
  IMAGE_RUNNER_PATH,
  IMAGE_RUNNER_PORT,
  imageRunnerPidLookup,
  imageRunnerRestartScript,
} from '../fixtures/runnerRestart';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

const RUNNER_RESTART_LOG = '/tmp/astrabox-runner-restart-resume.log';

interface TranscriptEntryDoc extends Record<string, unknown> {
  session_id?: string;
  uuid?: string | null;
  entry_json?: string;
}

interface RunnerProcessIdentity {
  pid: number;
  uid: string;
  exe: string;
  cwd: string;
  cmdline: string;
  runnerSha256: string;
  execUid: string;
  launcher: string;
}

/** Every native SessionStore row the platform holds for this conversation. */
function nativeEntries(sessionId: string): TranscriptEntryDoc[] {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .map((row) => row as TranscriptEntryDoc);
}

/** Native rows of one SDK record type whose payload mentions `marker`. */
function nativeEntriesMentioning(
  rows: TranscriptEntryDoc[],
  type: 'user' | 'assistant',
  marker: string,
): TranscriptEntryDoc[] {
  return rows.filter((row) => {
    if (typeof row.entry_json !== 'string') return false;
    try {
      const entry = JSON.parse(row.entry_json) as Record<string, unknown>;
      return String(entry.type || '') === type && row.entry_json.includes(marker);
    } catch {
      return false;
    }
  });
}

/**
 * Read the single image-baked runner's process identity from `/proc`.
 *
 * The same lookup the restart script uses finds exactly one runner, so a
 * lingering old process or a missing replacement fails here rather than being
 * averaged into "some runner answered".
 */
function readRunnerIdentity(sandbox: SandboxHandle, linuxUser: string): RunnerProcessIdentity {
  expect(linuxUser, 'the proc reader must be the verified workload account').toMatch(/^[a-z_][a-z0-9_-]*$/);
  const script = [
    'set -eu',
    imageRunnerPidLookup(),
    'pid="$1"',
    'printf \'pid=%s\\n\' "$pid"',
    'printf \'uid=%s\\n\' "$(awk \'/^Uid:/ { print $2 }\' "/proc/$pid/status")"',
    // These links use ptrace credential checks: container root without
    // CAP_SYS_PTRACE cannot read another uid's links, but their owner can.
    `exe="$(runuser -u ${linuxUser} -- readlink "/proc/$pid/exe")"`,
    `cwd="$(runuser -u ${linuxUser} -- readlink "/proc/$pid/cwd")"`,
    'printf \'exe=%s\\n\' "$exe"',
    'printf \'cwd=%s\\n\' "$cwd"',
    'printf \'cmdline=%s\\n\' "$(tr \'\\0\' \' \' < "/proc/$pid/cmdline")"',
    'printf \'runner_sha256=%s\\n\' "$(sha256sum "$runner_path" | cut -d \' \' -f 1)"',
    'printf \'exec_uid=%s\\n\' "$(id -u)"',
    `printf 'launcher=%s\\n' "$(test -x ${IMAGE_RUNNER_LAUNCHER} && echo present || echo missing)"`,
  ].join('\n');
  const raw = sandboxExec(sandbox, script);
  const fields = new Map<string, string>();
  for (const line of raw.split('\n')) {
    const separator = line.indexOf('=');
    if (separator <= 0) continue;
    fields.set(line.slice(0, separator), line.slice(separator + 1).trim());
  }
  const required = (key: string): string => {
    const value = fields.get(key);
    if (value === undefined || value === '') {
      throw new Error(`runner identity probe did not report ${key}; output=${raw.slice(0, 800)}`);
    }
    return value;
  };
  const pid = Number(required('pid'));
  if (!Number.isInteger(pid) || pid <= 1) {
    throw new Error(`runner identity probe reported an invalid pid; output=${raw.slice(0, 800)}`);
  }
  return {
    pid,
    uid: required('uid'),
    exe: required('exe'),
    cwd: required('cwd'),
    cmdline: required('cmdline'),
    runnerSha256: required('runner_sha256'),
    execUid: required('exec_uid'),
    launcher: required('launcher'),
  };
}

/** The substrate object a handle resolved to, for a same-box comparison. */
function physicalKey(handle: SandboxHandle): string {
  return handle.runtime === 'docker'
    ? `docker:${handle.container}`
    : `kubernetes:${handle.namespace}/${handle.pod}`;
}

test('the original conversation resumes on the same sandbox after its image-baked runner is terminated and relaunched', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const memoryToken = `RUNNER-RESTART-${randomBytes(6).toString('hex').toUpperCase()}`;
  const fileName = `runner-restart-${runId}.txt`;
  const fileContent = `runner restart marker ${runId}\n`;
  const plantPrompt =
    'Do not use tools. Remember this token, I will ask for it later in this conversation: '
    + `${memoryToken}. Reply with one short sentence acknowledging that you will remember it.`;
  const recallPrompt =
    'Do not use tools. Reply with only the token I asked you to remember earlier in this conversation.';

  // Conversation tenancy, not the campaign Agent's. This spec replaces the
  // runner process inside the box and asserts below that the session is the
  // box's sole occupant, which a shared-box isolation session can never be.
  const agent = await api.createColdTestAgent(`__e2e_runner_restart_${runId}`);
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);

  // ── Warm turn: the token becomes conversation data the engine must carry
  //    across the process loss. Its wording is not the oracle. ───────────────
  const planted = await api.sendTurn(sessionId, plantPrompt);
  expect(planted.errorText, 'the planting turn must complete without an error frame').toBeNull();
  const provisioned = await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && !String(session.current_turn_id || '').trim(),
  );
  const sandboxId = String(provisioned.sandbox_id || '').trim();
  expect(sandboxId, 'the planting turn must own a sandbox').not.toEqual('');
  expect(Boolean(provisioned.runtime_unavailable), 'the warm conversation must not be unavailable').toBe(false);
  const plantTurnId = String(provisioned.last_turn_id || '').trim();
  expect(plantTurnId, 'the planting turn must settle as the session last turn').not.toEqual('');
  expect(String(provisioned.last_turn_status || ''), 'the planting turn must complete').toBe('COMPLETED');

  // ── Ownership: whole-box conversation tenancy with a resident host runtime
  //    holding a live link to the process this spec is about to kill. ────────
  const warmDetail = await api.adminSessionDetail(sessionId);
  const identity = warmDetail.runtime_identity;
  expect(
    identity && typeof identity === 'object',
    'runner replacement requires the session runtime identity as ownership evidence',
  ).toBeTruthy();
  expect(
    String(identity?.isolated_session_id || '').trim(),
    'this fault must own the whole runner process, not a shared-sandbox sibling',
  ).toEqual('');
  expect(
    String(identity?.sandbox_id || '').trim(),
    'runtime_identity.sandbox_id must name the session sandbox',
  ).toBe(sandboxId);
  const linuxUser = String(identity?.linux_user || '').trim();
  expect(linuxUser, 'runtime_identity must name a safe workload account').toMatch(/^[a-z_][a-z0-9_-]*$/);
  const workspaceDir = String(identity?.workspace_dir || '').trim();
  expect(workspaceDir, 'runtime_identity must name a safe absolute workspace').toMatch(/^\/[A-Za-z0-9._/-]+$/);
  const nativeSessionKey = String(warmDetail.engine_session_key || '').trim();
  expect(nativeSessionKey, 'the warm conversation must persist its native session key before the fault').not.toEqual('');
  expect(
    warmDetail.has_local_runtime,
    'the host must hold a resident runtime for the warm conversation before the fault',
  ).toBe(true);

  // ── Native checkpoint: the planted input is in the platform-held
  //    SessionStore under this native session before anything is killed. ─────
  await expect.poll(
    () => nativeEntriesMentioning(nativeEntries(sessionId), 'user', memoryToken).length,
    {
      timeout: 60_000,
      intervals: [500, 1_000, 2_000],
      message: 'the planted user record must reach the transcript repository',
    },
  ).toBe(1);
  const entriesBefore = nativeEntries(sessionId);
  expect(entriesBefore.length, 'the warm turn must leave native records').toBeGreaterThan(0);
  for (const row of entriesBefore) {
    expect(row.session_id, 'every native record must belong to the session native key').toBe(nativeSessionKey);
  }
  const plantedEntry = nativeEntriesMentioning(entriesBefore, 'user', memoryToken)[0];
  expect(plantedEntry.uuid, 'the planted record must have its native identity').toBeTruthy();

  // ── File data: a marker only this box's workspace holds. ──────────────────
  await api.uploadFileText(sessionId, workspaceDir, fileName, fileContent);
  const filePath = `${workspaceDir}/${fileName}`;
  expect(await api.downloadFileText(sessionId, filePath), 'the marker file must be readable before the fault').toBe(fileContent);

  // ── Old process identity, read before the fault. ─────────────────────────
  const sandbox = await requireSandboxHandle(api, sandboxId);
  const workloadUid = sandboxExec(sandbox, `id -u ${linuxUser}`).trim();
  expect(workloadUid, 'the workload account must resolve to a uid inside the box').toMatch(/^[0-9]+$/);
  const before = readRunnerIdentity(sandbox, linuxUser);
  expect(before.execUid, 'relaunching through the image launcher needs a root exec for runuser').toBe('0');
  expect(before.launcher, 'the image must ship its runner launcher').toBe('present');
  expect(before.uid, 'the image-started runner runs as the session workload account').toBe(workloadUid);
  expect(before.cmdline, 'the image-started runner runs the installed runner file').toContain(IMAGE_RUNNER_PATH);
  test.info().annotations.push({
    type: 'runner_before_restart',
    description: `pid=${before.pid} uid=${before.uid} exe=${before.exe} cwd=${before.cwd} sha256=${before.runnerSha256} sandbox=${sandboxId} runtime=${sandbox.runtime}`,
  });

  // ── Fault: terminate the runner and relaunch it through the image's own
  //    launcher. No host eviction: the host still holds its link to the dead
  //    process, and nothing reads the session before the next message. ──────
  const restartEvidence = sandboxExec(
    sandbox,
    imageRunnerRestartScript({
      launch: IMAGE_RUNNER_LAUNCHER,
      log: RUNNER_RESTART_LOG,
      evidence: `printf 'old_pid=%s port=%s\\n' "$old_pid" '${IMAGE_RUNNER_PORT}'`,
      name: 'relaunched image runner',
    }),
    30_000,
  ).trim();
  expect(restartEvidence, 'the restart must terminate the process it found').toContain(`old_pid=${before.pid} `);
  const after = readRunnerIdentity(sandbox, linuxUser);
  expect(after.pid, 'the relaunch must replace the process').not.toBe(before.pid);
  expect(after.exe, 'the relaunched runner must run the same interpreter').toBe(before.exe);
  expect(after.cmdline, 'the relaunched runner must run the same command line').toBe(before.cmdline);
  expect(after.runnerSha256, 'the relaunched runner must run the same runner file').toBe(before.runnerSha256);
  expect(after.uid, 'the relaunched runner must run as the same workload account').toBe(before.uid);
  expect(after.cwd, 'the relaunched runner must start in the same working directory').toBe(before.cwd);
  test.info().annotations.push({
    type: 'runner_after_restart',
    description: `${restartEvidence} new_pid=${after.pid} launcher=${IMAGE_RUNNER_LAUNCHER}`,
  });

  // ── The next message. The reply must carry the token the dead process
  //    held; only the engine's own resume from its persisted history can. ────
  const recalled = await api.sendTurn(sessionId, recallPrompt);
  expect(recalled.errorText, 'the first message after the runner restart must complete without an error frame').toBeNull();
  expect(recalled.text, 'the relaunched runner must resume the original conversation, not start a new one').toContain(memoryToken);

  const settled = await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && !String(session.current_turn_id || '').trim(),
  );
  expect(
    String(settled.sandbox_id || '').trim(),
    'runner restart recovery should stay on the same session sandbox',
  ).toBe(sandboxId);
  expect(
    Boolean(settled.runtime_unavailable),
    'runner restart recovery should not mark runtime unavailable',
  ).toBe(false);
  expect(
    String(settled.last_error || '').trim(),
    'runner restart recovery should not leave a user-visible error',
  ).toBe('');
  const recallTurnId = String(settled.last_turn_id || '').trim();
  expect(recallTurnId, 'the resumed turn must settle as the session last turn').not.toEqual('');
  expect(recallTurnId, 'the resumed turn must be a new turn').not.toBe(plantTurnId);
  expect(String(settled.last_turn_status || ''), 'the resumed turn must complete').toBe('COMPLETED');

  // ── Same physical box, served by the relaunched process. ─────────────────
  const sandboxAfter = await requireSandboxHandle(api, sandboxId);
  expect(physicalKey(sandboxAfter), 'the session must still resolve to the same substrate object').toBe(physicalKey(sandbox));
  const served = readRunnerIdentity(sandbox, linuxUser);
  expect(served.pid, 'the relaunched runner must be the process that served the resumed turn').toBe(after.pid);
  expect(await api.downloadFileText(sessionId, filePath), 'the workspace marker must survive in the same box').toBe(fileContent);
  test.info().annotations.push({
    type: 'runner_restart_log',
    description: sandboxExec(sandbox, `tail -40 ${RUNNER_RESTART_LOG} || true`).trim().slice(0, 4_000),
  });

  // ── Native custody: same native session, original records retained by
  //    identity and payload, the resumed turn appended under the same key. ───
  const detailAfter = await api.adminSessionDetail(sessionId);
  expect(
    String(detailAfter.engine_session_key || '').trim(),
    'the resumed conversation must keep its native session key',
  ).toBe(nativeSessionKey);
  expect(
    String(detailAfter.runtime_identity?.isolated_session_id || '').trim(),
    'the resumed conversation must still own the whole box',
  ).toEqual('');
  expect(
    String(detailAfter.runtime_identity?.sandbox_id || '').trim(),
    'runtime_identity.sandbox_id must still name the same sandbox',
  ).toBe(sandboxId);
  expect(detailAfter.has_local_runtime, 'the host must hold a resident runtime after the resumed turn').toBe(true);
  const entriesAfter = nativeEntries(sessionId);
  for (const original of entriesBefore) {
    const retained = entriesAfter.filter(
      (row) => row.uuid === original.uuid && row.entry_json === original.entry_json,
    );
    expect(retained, `native record ${String(original.uuid)} must survive exactly once`).toHaveLength(1);
  }
  expect(entriesAfter.length, 'the resumed turn must append native records').toBeGreaterThan(entriesBefore.length);
  for (const row of entriesAfter) {
    expect(row.session_id, 'every native record must still belong to the session native key').toBe(nativeSessionKey);
  }
  expect(
    nativeEntriesMentioning(entriesAfter, 'user', recallPrompt).length,
    'the resumed input must be one native user record',
  ).toBe(1);
  expect(
    nativeEntriesMentioning(entriesAfter, 'assistant', memoryToken).length,
    'the resumed reply must be recorded natively under the same session',
  ).toBeGreaterThanOrEqual(1);

  // ── Durable history: two inputs, one reply each, the second carrying the
  //    token; the cold page renders the same. ───────────────────────────────
  const history = await api.getMessages(sessionId, 100);
  expect(history.has_more).toBe(false);
  expect(
    history.messages.filter((message) => message.role === 'user').map(messageText),
    'durable history must hold exactly the two submitted inputs in order',
  ).toEqual([plantPrompt, recallPrompt]);
  const plantReplies = history.messages.filter(
    (message) => message.role === 'assistant' && message.turn_id === plantTurnId,
  );
  expect(plantReplies, 'the planting turn must hold one durable reply').toHaveLength(1);
  const recallReplies = history.messages.filter(
    (message) => message.role === 'assistant' && message.turn_id === recallTurnId,
  );
  expect(recallReplies, 'the resumed turn must hold one durable reply').toHaveLength(1);
  expect(messageText(recallReplies[0]), 'the durable resumed reply must carry the planted token').toContain(memoryToken);
  const durableAssistantCount = history.messages.filter((message) => message.role === 'assistant').length;

  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
  await expect(page.getByTestId('user-message').filter({ hasText: plantPrompt })).toHaveCount(1);
  await expect(page.getByTestId('user-message').filter({ hasText: recallPrompt })).toHaveCount(1);
  await expect(page.getByTestId('assistant-message')).toHaveCount(durableAssistantCount);
  await expect(page.getByTestId('assistant-message').last()).toContainText(memoryToken);
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(pill).toHaveAttribute('data-state', 'READY');
  await expect(pill).toHaveAttribute('data-pulse', 'false');
});
