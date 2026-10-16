/**
 * E2E: a superseded runner link cannot strand the platform's next message.
 *
 * A sandbox id addresses one box, and the runner admits one host-link owner at
 * a time: normally the session's reader. This spec creates a second,
 * short-lived `attach` to the same runner, lets it take that ownership, and
 * disconnects it. The platform must then answer exactly one later message on
 * the same box, with the response recorded as ended on its own finish part.
 */
import { expect, test, type Response } from '@playwright/test';

import { AstraApi, messageText, type InputReceipt } from '../fixtures/astraApi';
import { sessionOutputRows, sessionResponseRows, type SessionResponseRow } from '../fixtures/dbOracle';
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
  runtimeHome: string,
  paths: AuxiliaryProbePaths,
): void {
  expect(
    sessionId,
    'the auxiliary attach requires the exact platform session identity',
  ).toMatch(/^[A-Za-z0-9_-]+$/);
  expect(Number.isSafeInteger(runnerPort) && runnerPort > 0).toBe(true);
  expect(runtimeHome, 'the exact runner seat must have a private home').toMatch(/^\//);

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
    `token_file = pathlib.Path(${JSON.stringify(runtimeHome)}) / ".astrabox-runner-token"`,
    'activation_token = token_file.read_text(encoding="utf-8").strip()',
    'if not activation_token:',
    '    raise RuntimeError("runner seat credential is empty")',
    '',
    'async def main():',
    '    async with websockets.connect(uri, ping_interval=None) as websocket:',
    '        await websocket.send(json.dumps({',
    '            "op": "attach",',
    '            "session_id": session_id,',
    '            "activation_token": activation_token,',
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

/** The input id the page's own send was accepted under. */
async function receiptOf(response: Response): Promise<string> {
  const receipt = ((await response.json()) as { data: InputReceipt }).data;
  expect(receipt.input_id, 'the platform must accept the message with an input id').toBeTruthy();
  return receipt.input_id;
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

  const firstInputId = await receiptOf(await sendPrompt(
    page,
    sessionId,
    `Do not use tools. Reply with exactly ${firstMarker}`,
  ));
  const firstAssistant = await api.waitForReplyTo(sessionId, firstInputId, { timeoutMs: TURN_BUDGET_MS });
  expect(messageText(firstAssistant), 'the warm-up response must finish on the real platform link')
    .toContain(firstMarker);
  const firstResponseId = String(firstAssistant.id || '').trim();
  expect(firstResponseId, 'the warm-up assistant message must be its response').not.toEqual('');

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

  const runtimeHome = String((runtimeIdentity as Record<string, unknown>).home_dir || '').trim();
  launchAuxiliaryAttach(handle, sessionId, runnerPort, runtimeHome, paths);
  await expect.poll(
    () => probeStatus(handle, paths),
    {
      timeout: AUXILIARY_ATTACH_BUDGET_MS,
      intervals: [250, 500],
      message: 'the auxiliary link must attach to this session\'s exact runner',
    },
  ).toContain('READY');

  // The seat credential authenticates the auxiliary link; the session hello
  // confirms its address. Disconnect its ownership before sending again.
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
  const secondInputId = await receiptOf(await sendPrompt(
    page,
    sessionId,
    `Do not use tools. Reply with exactly ${secondMarker}`,
  ));
  await expect(
    page.getByTestId('user-message').filter({ hasText: secondMarker }),
    'the post-disconnect input must enter the visible transcript exactly once',
  ).toHaveCount(1);

  const secondAssistant = await api.waitForReplyTo(sessionId, secondInputId, { timeoutMs: TURN_BUDGET_MS });
  expect(
    messageText(secondAssistant),
    'the surviving platform path must receive the post-disconnect reply',
  ).toContain(secondMarker);
  const secondResponseId = String(secondAssistant.id || '').trim();
  expect(secondResponseId, 'the post-disconnect assistant message must be its response').not.toEqual('');
  expect(secondResponseId, 'the post-disconnect input must open a new response').not.toBe(firstResponseId);

  let settleEvidence: Record<string, unknown> | null = null;
  await expect.poll(async () => {
    const detail = await api.getSession(sessionId);
    const assistantCount = await api.assistantCount(sessionId);
    settleEvidence = {
      state: String(detail.state || ''),
      open_responses: (detail.current_responses ?? []).length,
      last_response: `${detail.last_response?.response_id ?? ''}/${detail.last_response?.outcome ?? ''}`,
      assistant_delta: assistantCount - secondAssistantCount,
      sandbox_id: String(detail.sandbox_id || ''),
    };
    return settleEvidence;
  }, {
    timeout: TURN_BUDGET_MS,
    intervals: [500, 1_000],
    message: 'the accepted post-disconnect response must settle once on the same sandbox',
  }).toEqual({
    state: 'READY',
    open_responses: 0,
    last_response: `${secondResponseId}/completed`,
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

  // The platform's own record of the response: ended, and the last part it
  // relayed for it is the response's finish.
  let recorded: SessionResponseRow | undefined;
  await expect.poll(() => {
    recorded = sessionResponseRows(sessionId).find((row) => row.response_id === secondResponseId);
    return recorded ? `${recorded.status}/${recorded.outcome ?? ''}` : 'missing';
  }, {
    timeout: 120_000,
    message: 'the post-disconnect response must be recorded as ended',
  }).toBe('ended/completed');
  expect(
    recorded!.input_ids,
    'the recorded response must answer exactly the message sent after the disconnect',
  ).toEqual([secondInputId]);
  const recordedParts = sessionOutputRows(sessionId, secondResponseId).map(({ part }) => part);
  const finalDetail = await api.getSession(sessionId);
  const verdict = staleWriterReconnectVerdict({
    expectedSandboxId: sandboxId,
    actualSandboxId: String(finalDetail.sandbox_id || '').trim(),
    firstResponseId,
    secondResponseId,
    secondMarker,
    secondReplyText: messageText(secondAssistant),
    state: String(finalDetail.state || ''),
    openResponseCount: (finalDetail.current_responses ?? []).length,
    lastResponseId: String(finalDetail.last_response?.response_id || ''),
    lastResponseOutcome: String(finalDetail.last_response?.outcome || ''),
    assistantDelta: (await api.assistantCount(sessionId)) - secondAssistantCount,
    matchingAssistantCount: matchingAssistants.length,
    terminalType: String(recordedParts.at(-1)?.type || ''),
  });
  expect(
    verdict,
    'the superseding disconnect must preserve one complete response on the addressed sandbox',
  ).toEqual({
    addressedSandboxSurvived: true,
    openedDistinctResponse: true,
    replyReachedPlatform: true,
    completedExactlyOnce: true,
    settledReady: true,
    durableFinish: true,
  });

  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(pill, 'the post-disconnect response must leave the page settled').toHaveAttribute(
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
      first_response_id: firstResponseId,
      second_response_id: secondResponseId,
      settle: settleEvidence,
      auxiliary: probeStatus(handle, paths),
    }),
  });
});
