/**
 * E2E: a superseded runner link cannot strand the platform's next turn.
 *
 * A sandbox id addresses one box, and the runner admits one host-link owner at
 * a time. This spec creates a second, short-lived `attach` to the same runner,
 * lets it become that sole owner, and disconnects it. The platform must then
 * deliver exactly one later turn on the same box with a durable terminal proof.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { waitForTurnTerminalProof } from '../fixtures/dbOracle';
import {
  requireSandboxHandle,
  sandboxExec,
  type SandboxHandle,
} from '../fixtures/sandboxOps';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import {
  runnerPortFor,
  staleWriterReconnectVerdict,
} from '../fixtures/staleWriterReconnect';

const RUNNER_PROTOCOL = 'astrabox.runner-wire.v1';
const AUXILIARY_ATTACH_BUDGET_MS = 20_000;
const TURN_BUDGET_MS = 180_000;

const sessions = trackSessions();

interface AuxiliaryProbePaths {
  ready: string;
  release: string;
  disconnected: string;
  failed: string;
  log: string;
}

function auxiliaryProbePaths(runId: number): AuxiliaryProbePaths {
  const base = `/tmp/astrabox-e2e-stale-writer-${runId}`;
  return {
    ready: `${base}.ready.json`,
    release: `${base}.release`,
    disconnected: `${base}.disconnected`,
    failed: `${base}.failed`,
    log: `${base}.log`,
  };
}

function probeStatus(handle: SandboxHandle, paths: AuxiliaryProbePaths): string {
  return sandboxExec(
    handle,
    [
      `if [ -f ${paths.failed} ]; then`,
      `  printf 'FAILED\\n'; cat ${paths.failed}; printf '\\n'; cat ${paths.log}`,
      `elif [ -f ${paths.disconnected} ]; then`,
      `  printf 'DISCONNECTED\\n'; cat ${paths.disconnected}`,
      `elif [ -f ${paths.ready} ]; then`,
      `  printf 'READY\\n'; cat ${paths.ready}`,
      'else',
      `  printf 'WAITING\\n'; if [ -s ${paths.log} ]; then cat ${paths.log}; fi`,
      'fi',
    ].join('\n'),
  ).trim();
}

function launchAuxiliaryAttach(
  handle: SandboxHandle,
  sessionId: string,
  runnerPort: number,
  paths: AuxiliaryProbePaths,
): void {
  expect(
    sessionId,
    'the auxiliary attach requires the exact platform session identity',
  ).toMatch(/^[A-Za-z0-9_-]+$/);
  expect(Number.isSafeInteger(runnerPort) && runnerPort > 0).toBe(true);

  const python = [
    'import asyncio',
    'import json',
    'import pathlib',
    'import traceback',
    'import websockets',
    '',
    `session_id = ${JSON.stringify(sessionId)}`,
    `uri = ${JSON.stringify(`ws://127.0.0.1:${runnerPort}/`)}`,
    `ready = pathlib.Path(${JSON.stringify(paths.ready)})`,
    `release = pathlib.Path(${JSON.stringify(paths.release)})`,
    `disconnected = pathlib.Path(${JSON.stringify(paths.disconnected)})`,
    `failed = pathlib.Path(${JSON.stringify(paths.failed)})`,
    '',
    'async def main():',
    '    async with websockets.connect(uri, ping_interval=None) as websocket:',
    '        await websocket.send(json.dumps({',
    '            "op": "attach",',
    '            "session_id": session_id,',
    '            "last_seen_seq": 0,',
    '            "engine_requirements": {',
    '                "adapter": "claude_code",',
    '                "required_option_keys": [],',
    '                "permission_mode": "default",',
    '            },',
    '        }))',
    '        hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))',
    '        expected = {',
    '            "op": "hello",',
    `            "protocol": ${JSON.stringify(RUNNER_PROTOCOL)},`,
    '            "session_id": session_id,',
    '        }',
    '        observed = {key: hello.get(key) for key in expected}',
    '        if observed != expected:',
    '            raise RuntimeError(f"auxiliary attach reached the wrong runner: {hello!r}")',
    '        if not isinstance(hello.get("last_seq"), int):',
    '            raise RuntimeError(f"runner hello has no integer last_seq: {hello!r}")',
    '        ready.write_text(json.dumps(hello, sort_keys=True), encoding="utf-8")',
    '        deadline = asyncio.get_running_loop().time() + 45',
    '        while not release.exists():',
    '            if asyncio.get_running_loop().time() >= deadline:',
    '                raise TimeoutError("platform never released the auxiliary attach")',
    '            await asyncio.sleep(0.1)',
    '',
    'try:',
    '    asyncio.run(main())',
    'except BaseException:',
    '    failed.write_text(traceback.format_exc(), encoding="utf-8")',
    '    raise',
    'else:',
    '    disconnected.write_text("auxiliary host link closed", encoding="utf-8")',
  ].join('\n');

  sandboxExec(
    handle,
    [
      `rm -f ${paths.ready} ${paths.release} ${paths.disconnected} ${paths.failed} ${paths.log}`,
      `python3 - <<'PY' >${paths.log} 2>&1 &`,
      python,
      'PY',
      'printf "%s\\n" "$!"',
    ].join('\n'),
  );
}

test('platform can send after the superseding runner link disconnects', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });

  const runId = Date.now();
  const firstMarker = `STALE_WRITER_FIRST_${runId}`;
  const secondMarker = `STALE_WRITER_SECOND_${runId}`;

  await api.waitForSessionReady(sessionId);
  await openSessionView(page, sessionId);

  const firstAssistantCount = await api.assistantCount(sessionId);
  await sendPrompt(
    page,
    sessionId,
    `Do not use tools. Reply with exactly ${firstMarker}`,
  );
  const firstAssistant = await api.waitForAssistantMessageMatching(
    sessionId,
    firstAssistantCount,
    (message) => messageText(message).includes(firstMarker),
    TURN_BUDGET_MS,
  );
  expect(messageText(firstAssistant), 'the warm-up turn must finish on the real platform link')
    .toContain(firstMarker);
  const firstTurnId = String(firstAssistant.turn_id || '').trim();
  expect(firstTurnId, 'the warm-up assistant message must name its turn').not.toEqual('');

  const warm = await api.waitForSessionReady(sessionId);
  const sandboxId = String(warm.sandbox_id || '').trim();
  expect(sandboxId, 'the warm conversation must remain bound to one sandbox').not.toEqual('');
  const runtimeIdentity = (await api.adminSessionDetail(sessionId)).runtime_identity;
  expect(
    runtimeIdentity && typeof runtimeIdentity === 'object',
    `the warm session must expose its runtime identity; session=${JSON.stringify(warm)}`,
  ).toBeTruthy();
  const runnerPort = runnerPortFor(runtimeIdentity as Record<string, unknown>);
  const handle = await requireSandboxHandle(api, sandboxId);
  const paths = auxiliaryProbePaths(runId);

  launchAuxiliaryAttach(handle, sessionId, runnerPort, paths);
  await expect.poll(
    () => probeStatus(handle, paths),
    {
      timeout: AUXILIARY_ATTACH_BUDGET_MS,
      intervals: [250, 500],
      message: 'the auxiliary link must attach to this session\'s exact runner',
    },
  ).toContain('READY');

  // Disconnect the newest link. There is intentionally no generation token or
  // writer epoch here: the exact sandbox address plus the runner's session
  // hello is the identity proof, and one link owner is the whole arbitration.
  sandboxExec(handle, `touch ${paths.release}`);
  await expect.poll(
    () => probeStatus(handle, paths),
    {
      timeout: AUXILIARY_ATTACH_BUDGET_MS,
      intervals: [250, 500],
      message: 'the auxiliary owner must close before the platform sends again',
    },
  ).toContain('DISCONNECTED');

  const afterDisconnect = await api.getSession(sessionId);
  expect(
    String(afterDisconnect.sandbox_id || '').trim(),
    'link ownership changes must not replace the addressed sandbox',
  ).toBe(sandboxId);

  const secondAssistantCount = await api.assistantCount(sessionId);
  await sendPrompt(
    page,
    sessionId,
    `Do not use tools. Reply with exactly ${secondMarker}`,
  );
  await expect(
    page.getByTestId('user-message').filter({ hasText: secondMarker }),
    'the post-disconnect input must enter the visible transcript exactly once',
  ).toHaveCount(1);

  const secondAssistant = await api.waitForAssistantMessageMatching(
    sessionId,
    secondAssistantCount,
    (message) => messageText(message).includes(secondMarker),
    TURN_BUDGET_MS,
  );
  expect(
    messageText(secondAssistant),
    'the surviving platform path must receive the post-disconnect reply',
  ).toContain(secondMarker);
  const secondTurnId = String(secondAssistant.turn_id || '').trim();
  expect(secondTurnId, 'the post-disconnect assistant message must name its turn').not.toEqual('');
  expect(secondTurnId, 'the post-disconnect input must open a new turn').not.toBe(firstTurnId);

  let settleEvidence: Record<string, unknown> | null = null;
  await expect.poll(async () => {
    const detail = await api.getSession(sessionId);
    const assistantCount = await api.assistantCount(sessionId);
    settleEvidence = {
      state: String(detail.state || ''),
      current_turn_id: String(detail.current_turn_id || ''),
      last_turn_status: String(detail.last_turn_status || ''),
      assistant_delta: assistantCount - secondAssistantCount,
      sandbox_id: String(detail.sandbox_id || ''),
    };
    return settleEvidence;
  }, {
    timeout: TURN_BUDGET_MS,
    intervals: [500, 1_000],
    message: 'the accepted post-disconnect turn must settle once on the same sandbox',
  }).toEqual({
    state: 'READY',
    current_turn_id: '',
    last_turn_status: 'COMPLETED',
    assistant_delta: 1,
    sandbox_id: sandboxId,
  });

  const messages = (await api.getMessages(sessionId, 50)).messages || [];
  const matchingAssistants = messages.filter(
    (message) => message.role === 'assistant' && messageText(message).includes(secondMarker),
  );
  await expect(
    page.getByTestId('assistant-message').filter({ hasText: secondMarker }),
    'the user must see exactly one post-disconnect reply',
  ).toHaveCount(1);

  const terminal = await waitForTurnTerminalProof(
    sessionId,
    secondTurnId,
    'COMPLETED',
    120_000,
  );
  const terminalFrame =
    terminal.last_turn_terminal_frame && typeof terminal.last_turn_terminal_frame === 'object'
      ? (terminal.last_turn_terminal_frame as Record<string, unknown>)
      : {};
  const finalDetail = await api.getSession(sessionId);
  const verdict = staleWriterReconnectVerdict({
    expectedSandboxId: sandboxId,
    actualSandboxId: String(finalDetail.sandbox_id || '').trim(),
    firstTurnId,
    secondTurnId,
    secondMarker,
    secondReplyText: messageText(secondAssistant),
    state: String(finalDetail.state || ''),
    currentTurnId: String(finalDetail.current_turn_id || ''),
    lastTurnStatus: String(finalDetail.last_turn_status || ''),
    assistantDelta: (await api.assistantCount(sessionId)) - secondAssistantCount,
    matchingAssistantCount: matchingAssistants.length,
    terminalType: String(terminalFrame.type || ''),
  });
  expect(
    verdict,
    'the superseding disconnect must preserve one complete turn on the addressed sandbox',
  ).toEqual({
    addressedSandboxSurvived: true,
    openedDistinctTurn: true,
    replyReachedPlatform: true,
    completedExactlyOnce: true,
    settledReady: true,
    durableFinish: true,
  });

  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(pill, 'the post-disconnect turn must leave the page settled').toHaveAttribute(
    'data-pulse',
    'false',
  );
  await expect(pill, 'the conversation stays ready after link ownership recovery').toHaveAttribute(
    'data-state',
    'READY',
  );

  test.info().annotations.push({
    type: 'stale_writer_reconnect_evidence',
    description: JSON.stringify({
      sandbox_id: sandboxId,
      runner_port: runnerPort,
      first_turn_id: firstTurnId,
      second_turn_id: secondTurnId,
      settle: settleEvidence,
      auxiliary: probeStatus(handle, paths),
    }),
  });
});
