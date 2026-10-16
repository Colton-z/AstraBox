import fs from 'node:fs';

import { expect, test } from '@playwright/test';

import { messageParts } from '../fixtures/astraApi';
import { nativeArtifactCalls, expectStoredVersion } from '../fixtures/artifactsApi';
import { simplePage } from '../fixtures/artifactPages';
import {
  artifactFrame, artifactResources, exactToolPrompt, marker, openConversation, startArtifactScene,
} from '../fixtures/artifactScenario';
import {
  nativeTranscriptRows, postgresRows, sessionOutputRows, sessionResponseRows, sqlLiteral,
} from '../fixtures/dbOracle';
import { modeName } from '../fixtures/engineProfile';
import { parseTimeoutEnv } from '../fixtures/env';
import {
  clearFrameHoldFault, FRAME_HOLD_FAULT_DIR, frameHoldFaultPath,
  readFrameHoldFault, releaseFrameHoldFault,
} from '../fixtures/frameHold';
import { sendPrompt } from '../fixtures/sessionPage';

const resources = artifactResources();

interface Cursor {
  generation: string;
  position: { seq: number; index: number };
}
interface CutReceipt {
  fault: string;
  session_id: string;
  response_id: string;
  tool_call_id: string;
  card_ids: string[];
  cursor: Cursor;
}
interface ReaderFacts {
  generation: number;
  reader_cursor: Cursor | null;
  engine_session_key: string;
}
interface CardRecord {
  card_id: string;
  session_id: string;
  assistant_id: string | null;
  tool: string;
  kind: string;
  data: Record<string, unknown>;
}

function readerFacts(sessionId: string): ReaderFacts {
  const rows = postgresRows<ReaderFacts>(
    'SELECT r.generation, s.reader_cursor, s.engine_session_key '
      + 'FROM sessions s JOIN session_readers r USING (session_id) '
      + `WHERE s.session_id = ${sqlLiteral(sessionId)}`,
  );
  expect(rows, 'the suspended live reader must have a durable lease').toHaveLength(1);
  return rows[0]!;
}

function nativeResults(sessionId: string, callId: string): Record<string, unknown>[] {
  return nativeTranscriptRows(sessionId).filter((row) => row.subpath == null).flatMap((row) => {
    const entry = JSON.parse(String(row.entry_json)) as { message?: { content?: unknown } };
    const content = entry.message?.content;
    if (!Array.isArray(content)) return [];
    return (content as Record<string, unknown>[]).filter(
      (block) => block.type === 'tool_result' && block.tool_use_id === callId,
    );
  });
}

test('a native platform card result survives reader loss before its card lookup completes', async ({ page, request }) => {
  const scene = await startArtifactScene(page, request, resources, { engine: 'claude_code' });
  expect(scene.profile!.engine_kind).toBe('claude_code');
  await scene.api.setPermissionMode(scene.sessionId, modeName(scene.profile!, 'unattended'));
  await openConversation(page, scene.sessionId);
  const before = await scene.api.getSession(scene.sessionId);
  const title = marker('lookup crash artifact');
  const value = marker('lookup_crash');
  const content = simplePage(value);
  const faultPath = frameHoldFaultPath(scene.sessionId);
  fs.mkdirSync(FRAME_HOLD_FAULT_DIR, { recursive: true });
  fs.chmodSync(FRAME_HOLD_FAULT_DIR, 0o777);
  const temporary = `${faultPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify({
    faults: { detach_during_card_lookup: 1 },
    match: { session_id: scene.sessionId },
    release: false,
    consumed: [],
  }));
  fs.chmodSync(temporary, 0o644);
  fs.renameSync(temporary, faultPath);

  try {
    await sendPrompt(page, scene.sessionId, exactToolPrompt('create_artifact', {
      title, kind: 'html', content, capabilities: {},
    }));
    await expect.poll(() => readFrameHoldFault(faultPath)?.consumed.length ?? 0, {
      timeout: 60_000,
      message: 'this card-bearing result must reach the pre-lookup reader-loss boundary',
    }).toBe(1);
    const cut = readFrameHoldFault(faultPath)!.consumed[0] as CutReceipt;
    expect(cut).toMatchObject({ fault: 'detach_during_card_lookup', session_id: scene.sessionId });
    expect(cut.cursor?.generation).toBeTruthy();
    const leaseBefore = readerFacts(scene.sessionId);

    // Give the independent 50ms committer ample time to expose an early cursor commit.
    // This is a staged outage, not a wait for the expected recovery result.
    await page.waitForTimeout(500);
    const held = readerFacts(scene.sessionId);
    expect(held.generation, 'the original reader is still suspended, not yet replaced')
      .toBe(leaseBefore.generation);
    expect(held.reader_cursor?.generation).toBe(cut.cursor.generation);
    const position = held.reader_cursor!.position;
    expect(
      position.seq < cut.cursor.position.seq
        || (position.seq === cut.cursor.position.seq && position.index < cut.cursor.position.index),
      'the committed native cursor must not include the unresolved card-bearing result',
    ).toBe(true);
    expect(sessionOutputRows(scene.sessionId, cut.response_id).filter(({ part }) =>
      part.type === 'tool-output-available' && part.toolCallId === cut.tool_call_id),
    'the source result must stay uncommitted while its cards are unresolved').toEqual([]);

    await expect.poll(() => nativeArtifactCalls(scene.sessionId).filter(
      (call) => call.input.title === title,
    ), { timeout: 30_000 }).toHaveLength(1);
    const call = nativeArtifactCalls(scene.sessionId).find((item) => item.input.title === title)!;
    expect(call.name).toMatch(/create_artifact$/);
    expect(call.id, 'the suspended platform result belongs to the actual native publication')
      .toBe(cut.tool_call_id);
    expect(call.input).toMatchObject({ title, kind: 'html', content });
    await expect.poll(() => nativeResults(scene.sessionId, call.id), { timeout: 30_000 }).toHaveLength(1);
    const nativeResult = nativeResults(scene.sessionId, call.id)[0]!;
    expect(nativeResult.is_error ?? false).toBe(false);
    const nativeCardIds = [...new Set(JSON.stringify(nativeResult.content).match(/\bcrd_[0-9a-f]{24}\b/g))];
    expect(nativeCardIds, 'the supplier result must independently name a recorded card').toHaveLength(1);
    expect(cut.card_ids).toEqual(nativeCardIds);
    const records = postgresRows<CardRecord>(
      'SELECT card_id, session_id, assistant_id, tool, kind, data FROM tool_cards '
        + `WHERE card_id IN (${nativeCardIds.map(sqlLiteral).join(',')})`,
    );
    expect(records).toHaveLength(nativeCardIds.length);
    expect(records[0]).toMatchObject({
      card_id: nativeCardIds[0], session_id: scene.sessionId, assistant_id: null,
      tool: 'create_artifact', kind: 'artifact',
    });
    const artifacts = (await scene.artifacts.list({ q: title })).items.filter((item) => item.title === title);
    expect(artifacts, 'the real platform tool must have published one durable artifact').toHaveLength(1);
    const artifact = artifacts[0]!;
    resources.artifacts.push(artifact.artifact_id);
    expect(records[0]!.data.artifacts).toEqual([
      expect.objectContaining({ id: artifact.artifact_id, title, kind: 'html', version: 1 }),
    ]);
    await expectStoredVersion(scene.artifacts, artifact.artifact_id, 1, content);
    await test.info().attach('lookup-cut-independent-evidence', {
      body: JSON.stringify({ cut, leaseBefore, held, call, nativeResult, records, artifact }),
      contentType: 'application/json',
    });

    // Release injects one stream loss before lookup returns. Only this reader dies;
    // normal lease takeover and native journal replay must recover the owed parts.
    releaseFrameHoldFault(faultPath);
    await expect.poll(() => Number(readerFacts(scene.sessionId).generation), {
      timeout: 60_000, message: 'a new reader must take over the surviving native conversation',
    }).toBeGreaterThan(Number(leaseBefore.generation));
    const readyBudget = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 180_000);
    const responseDeadline = Date.now() + readyBudget;
    const ready = await scene.api.waitForSessionReady(scene.sessionId, readyBudget);
    expect(ready.sandbox_id, 'reader loss must not replace compute').toBe(before.sandbox_id);
    expect(readerFacts(scene.sessionId).engine_session_key).toBe(leaseBefore.engine_session_key);
    await expect.poll(
      () => sessionResponseRows(scene.sessionId).find((row) => row.response_id === cut.response_id),
      {
        timeout: Math.max(1, responseDeadline - Date.now()),
        message: 'the original response must reach its native ending after reader takeover',
      },
    ).toMatchObject({ status: 'ended', outcome: 'completed', error: null });
    const output = sessionOutputRows(scene.sessionId, cut.response_id);
    expect(output.filter(({ part }) => part.type === 'tool-output-available'
      && part.toolCallId === call.id), 'the native result is committed once after recovery').toHaveLength(1);
    const result = output.find(({ part }) => part.type === 'tool-output-available' && part.toolCallId === call.id)!;
    for (const id of nativeCardIds) expect(JSON.stringify(result.part.output)).toContain(id);
    const cards = output.filter(({ part }) => part.type === 'data-tool-card').map(({ part }) => part);
    expect(cards, 'every native card has one durable, stable part on the same response').toEqual(
      records.map((record) => expect.objectContaining({
        id: `tool-card:${call.id}:${record.card_id}`,
        data: { toolCallId: call.id, cardId: record.card_id, kind: record.kind, ...record.data },
      })),
    );
    expect(nativeArtifactCalls(scene.sessionId).filter((item) => item.input.title === title),
      'reader recovery must not repeat the native publication').toHaveLength(1);
    const history = await scene.api.getHistoryBlocks(scene.sessionId, 100);
    expect(history.messages.flatMap((message) => messageParts(message, 'data-tool-card')),
      'history must expose the same durable card parts, outside collapsed tool details')
      .toEqual(cards.map(({ type, id, data }) => ({ type, id, data })));

    const card = page.getByTestId('artifact-tool-card').filter({ hasText: title });
    await expect(card, 'the recovered stream must show the actual artifact card').toHaveCount(1);
    await expect(card).toBeVisible();
    await card.getByRole('button', { name: 'Open', exact: true }).click();
    await expect(artifactFrame(page).locator('#marker')).toHaveText(value);
    await page.reload();
    await expect(card, 'cold history must retain exactly the same card').toHaveCount(1);
    await expect(card).toBeVisible();
    await card.getByRole('button', { name: 'Open', exact: true }).click();
    await expect(artifactFrame(page).locator('#marker')).toHaveText(value);
    await test.info().attach('lookup-cut-recovered-evidence', {
      body: JSON.stringify({ reader: readerFacts(scene.sessionId), output, records }),
      contentType: 'application/json',
    });
  } finally {
    try {
      await test.info().attach('lookup-cut-fault-receipt', {
        body: JSON.stringify(readFrameHoldFault(faultPath)), contentType: 'application/json',
      });
    } finally {
      releaseFrameHoldFault(faultPath);
      clearFrameHoldFault(faultPath);
    }
  }
});
