/** Real spool acceptance across lost/malformed ACK and four gateway failures. */
import { isDeepStrictEqual } from 'node:util';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import { requireSandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  transcriptAppendFault, type AppendFaultEvidence,
} from '../fixtures/transcriptAppendAckLoss';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function batchRows(appendId: string) {
  return documentsByField('transcript_entries', '$.append_id', appendId)
    .sort((a, b) => Number(a.batch_index) - Number(b.batch_index));
}

function requireBatch(evidence: AppendFaultEvidence | null): AppendFaultEvidence & {
  append_id: string; entries: Record<string, unknown>[];
} {
  if (!evidence?.append_id || !evidence.entries?.length) {
    throw new Error('the real native input did not identify a nonempty append batch');
  }
  return evidence as AppendFaultEvidence & {
    append_id: string; entries: Record<string, unknown>[];
  };
}

function assertExactStoredBatch(sessionId: string, evidence: ReturnType<typeof requireBatch>) {
  const rows = batchRows(evidence.append_id);
  expect(rows, 'each original batch entry must exist once, including after retry')
    .toHaveLength(evidence.entries.length);
  expect(rows.map((row) => row.platform_session_id)).toEqual(rows.map(() => sessionId));
  expect(rows.map((row) => Number(row.batch_index)))
    .toEqual(rows.map((_, index) => index));
  expect(rows.map((row) => JSON.parse(String(row.entry_json)))).toEqual(evidence.entries);
  return rows;
}

test('the real SessionStore retries identical appends after lost or malformed ACK and repeated 502 without duplicates', async ({
  page, request,
}) => {
  const api = new AstraApi(request);
  const unique = `${Date.now()}_${test.info().workerIndex}`;
  const agent = await api.createColdTestAgent(`__e2e_append_ack_${unique}`);
  agentId = agent.agent_id;
  const session = await api.startConversation(agentId);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const warmup = await api.sendTurn(sessionId, 'Briefly greet me without using tools.', 90_000);
  expect(warmup.errorText).toBeNull();
  expect(warmup.text.trim()).not.toEqual('');
  const ready = await api.waitForSession(sessionId,
    (value) => value.state === 'READY' && !value.current_turn_id, 60_000);
  const detail = await api.adminSessionDetail(sessionId);
  expect(String(detail.runtime_identity?.isolated_session_id || ''),
    'the fixture must own the whole runner, never a shared-box sibling').toEqual('');
  const sandbox = await requireSandboxHandle(api, String(ready.sandbox_id));
  const fault = transcriptAppendFault(sandbox, sessionId);
  const prompts: string[] = [];
  const receipts: Record<string, unknown>[] = [];
  let installed = false;
  try {
    await api.adminEvictRuntime(sessionId);
    // Installation changes only the test-owned launcher input, not image files.
    receipts.push({ restart: fault.install() });
    installed = true;
    for (const mode of ['ack', '502', 'malformed'] as const) {
      const marker = `APPEND_${mode}_${unique}`;
      const prompt = `Conversation label: ${marker}. Explain briefly what a notebook is. Do not use tools.`;
      prompts.push(prompt);
      fault.arm(mode, marker);
      // Retain rejection as evidence immediately, avoiding an unhandled promise
      // while the test is independently inspecting the pending transport fault.
      const response = api.sendTurn(sessionId, prompt, 90_000).then(
        (value) => ({ value, error: null }),
        (error: Error) => ({ value: null, error: error.message }),
      );
      const heldStage = mode === 'ack' ? 'committed-awaiting-release' : 'retry-awaiting-release';
      const readStage = () => {
        const state = fault.read(mode);
        if (state?.errors.length) {
          throw new Error(`transcript ${mode} response probe failed: ${state.errors.join('; ')}`);
        }
        return state?.stage;
      };
      await expect.poll(readStage, {
        timeout: 45_000, intervals: [200, 500],
        message: `real ${mode} append must reach its exact response boundary`,
      }).toBe(heldStage);
      const held = requireBatch(fault.read(mode));
      expect(held.errors).toEqual([]);
      expect(fault.pending(held.append_id), 'unacknowledged native batch stays on disk')
        .toHaveLength(1);
      expect(held.sdk_accepts.filter((item) =>
        item.payload_sha256 === held.attempts[0].payload_sha256),
      'the SDK append must return inside its real one-second caller budget')
        .toEqual([expect.objectContaining({ elapsed_seconds: expect.any(Number) })]);
      expect(held.sdk_accepts.every((item) => item.elapsed_seconds < 1)).toBe(true);
      let committedBeforeRetry: Record<string, unknown>[] = [];
      if (mode === 'ack') {
        expect(held.attempts.map((item) => item.status)).toEqual([200]);
        committedBeforeRetry = assertExactStoredBatch(sessionId, held);
      } else if (mode === 'malformed') {
        expect(held.attempts.map((item) => item.status)).toEqual([200, undefined]);
        expect(held.attempts[0].injected_malformed_ack).toBe(true);
        committedBeforeRetry = assertExactStoredBatch(sessionId, held);
      } else {
        expect(held.attempts.map((item) => item.status)).toEqual([502, 502, 502, 502, undefined]);
        expect(batchRows(held.append_id), 'four rejected attempts have not written the batch').toEqual([]);
      }
      fault.release(mode);
      await expect.poll(readStage, {
        timeout: 30_000, intervals: [200, 500],
        message: 'the original production flusher must obtain a real successful ACK',
      }).toBe('acknowledged');
      await expect.poll(() => fault.pending(held.append_id).length, {
        timeout: 15_000, intervals: [200, 500],
      }).toBe(0);
      const complete = requireBatch(fault.read(mode));
      expect(complete.errors).toEqual([]);
      expect(complete.attempts.map((item) => item.status))
        .toEqual(mode === '502' ? [502, 502, 502, 502, 200] : [200, 200]);
      for (const attempt of complete.attempts) {
        expect(attempt.append_id).toBe(held.append_id);
        expect(attempt.request_body_sha256).toBe(complete.attempts[0].request_body_sha256);
        expect(attempt.payload_sha256).toBe(complete.attempts[0].payload_sha256);
        expect(attempt.declared_payload_sha256).toBe(attempt.payload_sha256);
      }
      if (mode === 'ack') {
        expect(complete.attempts.map((item) => item.injected_timeout_after_commit)).toEqual([true, false]);
        expect(complete.attempts[1].store_sequence).toBe(complete.attempts[0].store_sequence);
      } else if (mode === 'malformed') {
        expect(complete.attempts.map((item) => item.injected_malformed_ack)).toEqual([true, false]);
        expect(complete.attempts[1].store_sequence).toBe(complete.attempts[0].store_sequence);
      } else {
        expect(complete.attempts.map((item) => item.injected_transient_http)).toEqual([true, true, true, true, false]);
      }
      const after = assertExactStoredBatch(sessionId, complete);
      if (mode !== '502') expect(after).toEqual(committedBeforeRetry);
      const nativeInputs = documentsByField('transcript_entries', '$.platform_session_id', sessionId)
        .filter((row) => {
          const entry = JSON.parse(String(row.entry_json));
          return entry.type === 'user' && JSON.stringify(entry).includes(marker);
        });
      expect(nativeInputs, 'no second append identity may hide a duplicate native input').toHaveLength(1);
      const reply = await response;
      expect(reply.error, 'original user request must complete, not only database repair').toBeNull();
      expect(reply.value?.errorText).toBeNull();
      expect(reply.value?.text.trim()).toBeTruthy();
      await api.waitForSession(sessionId,
        (value) => value.state === 'READY' && !value.current_turn_id, 30_000);
      const exported = (await api.adminSessionTranscript(sessionId, 30_000))
        .split('\n').filter((line) => line.trim()).map((line) => JSON.parse(line));
      for (const entry of complete.entries) {
        expect(exported.filter((item) => isDeepStrictEqual(item, entry)),
          'the real transcript read API must return each unchanged native entry once').toHaveLength(1);
      }
      receipts.push({
        mode, appendId: complete.append_id, entryCount: complete.entries.length,
        attempts: complete.attempts, sdkAccepts: complete.sdk_accepts,
        committedBeforeRetry: committedBeforeRetry.map((row) => row._id),
        committedAfter: after.map((row) => row._id),
      });
    }
    const history = await api.getMessages(sessionId, 50);
    const users = history.messages.filter((message) => message.role === 'user').map(messageText);
    for (const prompt of prompts) expect(users.filter((text) => text === prompt)).toHaveLength(1);
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();
    for (const prompt of prompts) {
      await expect(page.getByTestId('user-message').filter({ hasText: prompt })).toHaveCount(1);
    }
  } finally {
    try {
      if (installed) {
        fault.disarm();
        for (const mode of ['ack', '502', 'malformed'] as const) {
          const state = fault.read(mode);
          if (state) receipts.push({ ...state, entries: undefined });
        }
      }
    } finally {
      await test.info().attach('transcript-append-ack-loss', {
        body: JSON.stringify({ sessionId, receipts }), contentType: 'application/json',
      });
    }
  }
});
