/** Replay recorded diagnostic shapes; the native adapter and deployed history stay separate from chat content. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { postgresTextRows, sessionOutputRows, sqlLiteral } from '../fixtures/dbOracle';
import { POSTGRES_CONTAINER_HANDLE, SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessionId = randomUUID();
const responseId = randomUUID();
const sql = (statement: string) => postgresTextRows(
  requireServiceContainer(POSTGRES_CONTAINER_HANDLE), process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
  process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', statement,
);
onPassOnly(async () => { sql(`DELETE FROM sessions WHERE session_id=${sqlLiteral(sessionId)}`); });

test('Claude diagnostics remain operator evidence and recorded history renders without diagnostic cards', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const user = await api.data<{ user_id: string }>('GET', '/user/current');
  const agents = await api.data<Array<{ agent_id: string }>>('GET', '/agents');
  expect(agents.length).toBeGreaterThan(0);
  const marker = `DIAGNOSTIC_REPLAY_${randomUUID()}`;
  // The three kinds and counts come from the reported production snapshot.
  // Payloads contain no business data. This does not execute tools or compact a model session.
  const diagnostics = [
    ...Array.from({ length: 172 }, (_, index) => ({ type: 'tool_progress', heartbeat: true,
      tool_name: 'Bash', elapsed_time_seconds: 120 + index * 30 })),
    ...Array.from({ length: 10 }, () => ({ type: 'background_tasks_changed' })),
    ...Array.from({ length: 2 }, () => ({ type: 'compact_boundary' })),
  ];
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const translated = JSON.parse(execFileSync('docker', ['exec', '-i', server, 'python', '-c', `
import json,sys,os
from astrabox.core.service.orchestrator.engine.claude_runtime_client import _Translation
from astrabox.core.service.orchestrator.engine.runtime import RuntimeDiagnostic
from astrabox.core.service.orchestrator.session_runtime.ui_message import fold_message,public_part
source=json.load(sys.stdin)
prefix=[{'type':'data-raw-event','data':{'event_type':'claude_code.sdk','subtype':row['type'],'raw':row}} for row in source]
prefix += [{'type':'text-start','id':'continued-text'}, {'type':'text-delta','id':'continued-text','delta':'prefix'}]
history=fold_message('running-response',prefix)
assert history['metadata']['continuation_parts']==[{'index':0,'id':'continued-text'}], history
assert len(history['parts'])==1 and history['parts'][0]['type']=='text' and history['parts'][0]['text']=='prefix', history
assert all(public_part(part,shared=False) is None for part in prefix[:-2])
results=[]
for active in (False, True):
    tr=_Translation()
    if active: tr._start('recorded-response','input',('recorded-input',),interrupt_target='recorded-input')
    for seq,native in enumerate(source,1):
        for message in ({'__sdk_type':'NativeSDKMessage','data':native},
                        {'__sdk_type':'SystemMessage','subtype':native['type'],'data':native}):
            events=tr.translate({'op':'event','seq':seq,'message':message})
            assert len(events)==1 and isinstance(events[0],RuntimeDiagnostic), events
            assert events[0].event_type=='claude_code.sdk.'+native['type']
            assert events[0].raw==native or events[0].raw==message
    assert tr.response_id==('recorded-response' if active else None)
    results.append({'active':active,'diagnostics':len(source)*2,'response_id':tr.response_id})
print(json.dumps({'runtime':results,'environment':os.environ['ASTRABOX_ENVIRONMENT_NAME']}))
`], { input: JSON.stringify(diagnostics), encoding: 'utf8', timeout: 60_000 }));
  expect(translated.runtime).toEqual([
    { active: false, diagnostics: 368, response_id: null },
    { active: true, diagnostics: 368, response_id: 'recorded-response' },
  ]);

  const raw = (subtype: string, value: unknown) => ({ type: 'data-raw-event',
    data: { event_type: 'claude_code.sdk', subtype, raw: value } });
  const chunks = [
    { type: 'start', messageId: responseId },
    { type: 'reasoning-start', id: 'reasoning' },
    { type: 'reasoning-delta', id: 'reasoning', delta: 'Recorded reasoning remains available.' },
    { type: 'reasoning-end', id: 'reasoning' },
    { type: 'tool-input-available', toolCallId: 'recorded-tool', toolName: 'Bash', dynamic: true,
      input: { command: 'recorded command' } },
    ...diagnostics.map((native) => raw(native.type, native)),
    { type: 'tool-output-available', toolCallId: 'recorded-tool', dynamic: true, output: marker },
    { type: 'text-start', id: 'answer' },
    { type: 'text-delta', id: 'answer', delta: marker },
    { type: 'text-end', id: 'answer' },
    raw('assistant', { content: 'Native local-command presentation' }),
    raw('unrecognized-presentation', { content: 'An unclassified historical event remains inspectable.' }),
    { type: 'data-result', data: { duration_ms: 270000, usage: { input_tokens: 10, output_tokens: 20 } } },
    { type: 'finish', finishReason: 'stop' },
  ];
  sql(`INSERT INTO sessions (session_id, environment_name, user_id, session_kind, workspace_kind, agent_id, engine_kind, title, state) `
    + `VALUES (${sqlLiteral(sessionId)}, ${sqlLiteral(translated.environment)}, ${sqlLiteral(user.user_id)}, 'agent_chat', 'agent', ${sqlLiteral(agents[0]!.agent_id)}, 'claude_code', 'Recorded diagnostic replay', 'READY'); `
    + `INSERT INTO session_responses (session_id, response_id, origin, status, outcome, ended_at, conversation_order) `
    + `VALUES (${sqlLiteral(sessionId)}, ${sqlLiteral(responseId)}, 'engine', 'ended', 'completed', now(), 1); `
    + `INSERT INTO session_output (session_id, seq, response_id, part) VALUES `
    + chunks.map((part, index) => `(${sqlLiteral(sessionId)}, ${index + 1}, ${sqlLiteral(responseId)}, ${sqlLiteral(JSON.stringify(part))})`).join(','));
  const stored = sessionOutputRows(sessionId);
  expect(stored.map((row) => row.part)).toEqual(chunks);
  for (let read = 0; read < 2; read += 1) {
    await openSessionView(page, sessionId);
    await expect(page.getByTestId('assistant-text').filter({ hasText: marker })).toHaveCount(1);
    const cards = page.getByTestId('raw-event-card');
    for (const subtype of ['tool_progress', 'background_tasks_changed', 'compact_boundary']) {
      await expect(cards.filter({ hasText: `claude_code.sdk / ${subtype}` })).toHaveCount(0);
    }
    await expect(cards.filter({ hasText: 'claude_code.sdk / assistant' })).toHaveCount(1);
    await expect(cards.filter({ hasText: 'claude_code.sdk / unrecognized-presentation' })).toHaveCount(1);
    await revealAssistantProcess(page);
    const tool = page.locator('[data-tool-call-id="recorded-tool"]');
    await expect(tool).toHaveCount(1);
    await tool.locator('[data-slot="collapsible-trigger"]').click();
    await expect(tool).toContainText(marker);
    const reasoning = page.getByTestId('reasoning-part');
    await reasoning.getByRole('button').click();
    await expect(reasoning).toContainText('Recorded reasoning remains available.');
    const history = await api.getHistoryBlocks(sessionId, 50);
    expect(JSON.stringify(history)).not.toContain('elapsed_time_seconds');
    expect(sessionOutputRows(sessionId)).toEqual(stored);
  }
  await info.attach('diagnostic-replay', { body: JSON.stringify({ translated, originalDiagnosticCount: diagnostics.length,
    stored: sessionOutputRows(sessionId), history: await api.getHistoryBlocks(sessionId, 50) }), contentType: 'application/json' });
  await page.screenshot({ path: info.outputPath('diagnostic-history.png'), fullPage: true });
});
