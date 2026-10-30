/** An old counted Result cursor resumes the same live Claude conversation after compaction. */
import { spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { nativeTranscriptRows, postgresRows, sessionInputRows, sessionOutputRows,
  sessionResponseRows, sqlLiteral } from '../fixtures/dbOracle';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => { if (agentId) await new AstraApi(request).deleteAgent(agentId); });
const server = () => requireServiceContainer(SERVER_CONTAINER_HANDLE);
const docker = (...args: string[]) => {
  const result = spawnSync('docker', args, { encoding: 'utf8', timeout: 30_000 });
  if (result.status !== 0) throw new Error(String(result.stderr || result.error || result.stdout));
  return result.stdout + result.stderr;
};

function boundary(sid: string, action: 'arm' | 'read' | 'release') {
  return JSON.parse(docker('exec', server(), 'python', '-c', `
import json,os,pathlib,sys
sid,action=sys.argv[1:]
assert os.environ['ASTRABOX_E2E_FAULTS']=='1'
p=pathlib.Path(os.environ['ASTRABOX_E2E_FAULT_FILE']+'.d')/('compacted-result-'+sid+'.json')
if action in ('arm','release'):
 p.parent.mkdir(parents=True,exist_ok=True)
 value={'faults':{'hold_reader_boundary':1},'match':{'session_id':sid,'phase':'connected'},'release':False,'consumed':[]} if action=='arm' else {**json.loads(p.read_text()),'release':True}
 p.write_text(json.dumps(value));p.chmod(0o644)
print(p.read_text() if p.exists() else '{}')
`, sid, action));
}

function processes(handle: SandboxHandle, uid: number) {
  return JSON.parse(sandboxExec(handle, `python3 - <<'PY'
import json,os,pathlib,pwd
account=pwd.getpwuid(${uid})
if os.geteuid()!=account.pw_uid:
 os.setgid(account.pw_gid);os.setuid(account.pw_uid)
found=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  args=(p/'cmdline').read_bytes().split(bytes([0]))
  runner=args[-2:]==[b'/opt/astrabox/sandbox_runner.py',b'']
  cli=os.path.samefile(p/'exe','/usr/local/bin/claude')
  if runner or cli:found.append({'kind':'runner' if runner else 'cli','pid':int(p.name),'start':(p/'stat').read_text().split()[21]})
 except (FileNotFoundError,ProcessLookupError,PermissionError):pass
print(json.dumps(sorted(found,key=lambda x:x['kind'])))
PY`));
}

function retainedResult(handle: SandboxHandle) {
  if (handle.runtime !== 'docker') throw new Error('this selected old-image fixture requires the declared Docker testbed');
  const lines = sandboxExec(handle, 'cat /tmp/astrabox_runner.log').split('\n');
  const prefix = 'E2E_COMPACTED_RESULT:';
  const line = lines.filter((value) => value.includes(prefix)).at(-1);
  return line ? JSON.parse(line.slice(line.indexOf(prefix) + prefix.length)) : null;
}

test('an already compacted Result keeps the original reply and consumes its pending input once', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const resumed = (process.env.ASTRABOX_E2E_RETAINED_CURSOR_SESSION || '').trim();
  const checkpointFile = process.env.ASTRABOX_E2E_CURSOR_CHECKPOINT_FILE;
  const saved = checkpointFile ? JSON.parse(readFileSync(checkpointFile, 'utf8')) : null;
  if (saved) expect(resumed).not.toBe('');
  const previous = resumed ? sessionInputRows(resumed) : [];
  if (resumed) expect(previous).toHaveLength(saved ? 2 : 1);
  const marker = resumed ? previous[0]!.content.match(/FIRST_([0-9a-f]{32})/)?.[1]
    : randomUUID().replace(/-/g, '');
  expect(marker).toMatch(/^[0-9a-f]{32}$/);
  const first = `FIRST_${marker}`;
  const second = `SECOND_${marker}`;
  const path = `/workspace/cursor-${marker}.txt`;
  agentId = resumed ? String((await api.getSession(resumed)).agent_id)
    : (await api.createColdTestAgent(`__e2e_compacted_cursor_${marker}`)).agent_id;
  expect((await api.getAgent(agentId)).name).toMatch(/^__e2e_compacted_cursor_/);
  const sid = resumed || (await api.startConversation(agentId)).session_id;
  sessions.push(sid);
  if (!resumed) await api.waitForSessionReady(sid, 60_000);
  const runtime = await api.adminSessionDetail(sid);
  const uid = Number(runtime.runtime_identity?.uid);
  expect(uid).toBeGreaterThan(0);
  const handle = await requireSandboxHandle(api, String(runtime.sandbox_id));
  const initialProcesses = saved?.initialProcesses ?? processes(handle, uid);
  expect(initialProcesses.map((p: { kind: string }) => p.kind)).toEqual(['cli', 'runner']);
  await openSessionView(page, sid);
  const firstPrompt = `Use one Bash call with exactly this command: \`printf '${first}\\n' > ${path} && cat ${path}\`. Then reply only ${first}. No other tools or delegation.`;
  if (resumed) expect(previous[0]!.content).toBe(firstPrompt);
  const input = resumed ? previous[0]! : await api.postTurnInput(sid, firstPrompt);
  const reply = await api.waitForReplyTo(sid, input.input_id, { timeoutMs: 60_000 });
  expect(reply.metadata?.outcome).toBe('completed');
  expect(messageText(reply)).toContain(first);
  await expect(page.locator(`[data-message-id="${reply.id}"]`)).toContainText(first);
  let retained: Record<string, any> | null = saved?.retained ?? null;
  if (!saved) await expect.poll(() => { retained = retainedResult(handle); return retained?.message?.__sdk_type; },
    { timeout: 15_000 }).toBe('ResultMessage');
  const checkpoint = saved?.checkpoint ?? postgresRows<{ reader_cursor: { position: { seq: number; index: number } } }>(
    `SELECT reader_cursor FROM sessions WHERE session_id=${sqlLiteral(sid)}`,
  )[0]!.reader_cursor;
  expect(checkpoint.position.seq).toBe(retained!.seq);
  expect(checkpoint.position.index).toBeLessThan(1 << 30);
  expect(retained!.message.result).toContain(first);
  const original = saved?.original ?? { inputs: sessionInputRows(sid), responses: sessionResponseRows(sid),
    output: sessionOutputRows(sid), native: nativeTranscriptRows(sid) };
  expect(original.responses).toHaveLength(1);
  expect(original.responses[0]!.response_id).toBe(reply.id);

  // Hold the replacement host before it reads or delivers, then accept the original pending input.
  const target = process.env.ASTRABOX_E2E_CLAUDE_CLIENT_BIND_FILE;
  const candidate = process.env.ASTRABOX_E2E_CLAUDE_CLIENT_CANDIDATE_FILE;
  if (!target || !candidate) throw new Error('the selected upgrade fixture must declare both client source files');
  const bytes = readFileSync(candidate);
  if (saved) {
    expect(saved.candidateSha256).toBe(createHash('sha256').update(bytes).digest('hex'));
    expect(readFileSync(target).equals(bytes)).toBe(true);
    await info.attach('continued-checkpoint', { path: checkpointFile!, contentType: 'application/json' });
  } else if (readFileSync(target).equals(bytes)) {
    const beforeHostStart = JSON.parse(readFileSync(`${target}.before-host-start.json`, 'utf8'));
    expect(beforeHostStart.session_id).toBe(sid);
    expect(beforeHostStart.sandbox_id).toBe(handle.sandboxId);
    expect(beforeHostStart.reader_cursor).toEqual(checkpoint);
    expect(initialProcesses).toEqual(beforeHostStart.processes);
  } else {
    const host = server();
    boundary(sid, 'arm');
    docker('stop', '--time', '5', host);
    writeFileSync(target, bytes);
    docker('start', host);
    await expect.poll(async () => (await request.get('/readyz')).status(), { timeout: 30_000 }).toBe(200);
  }
  const secondPrompt = `Use one Bash call with exactly this command: \`printf '${second}\\n' >> ${path} && cat ${path}\`. Then reply only ${second}. No other tools or delegation.`;
  let pending = saved?.pending;
  if (!saved) {
    await expect.poll(() => boundary(sid, 'read').consumed?.length, { timeout: 20_000 }).toBe(1);
    pending = await api.postTurnInput(sid, secondPrompt);
    expect(sessionInputRows(sid).find((row) => row.input_id === pending.input_id)?.state).toBe('pending');
    expect(sessionResponseRows(sid)).toEqual(original.responses);
    await info.attach('old-compacted-checkpoint', { body: JSON.stringify({ checkpoint, retained, original,
      initialProcesses, pending, candidateSha256: createHash('sha256').update(bytes).digest('hex') }), contentType: 'application/json' });
    boundary(sid, 'release');
  }
  const next = await api.waitForReplyTo(sid, pending.input_id, { timeoutMs: 60_000 });
  expect(next.metadata?.outcome).toBe('completed');
  expect(messageText(next)).toContain(second);
  expect((await api.adminSessionDetail(sid)).sandbox_id).toBe(handle.sandboxId);
  expect(processes(handle, uid)).toEqual(initialProcesses);
  expect(await api.downloadFileText(sid, path)).toBe(`${first}\n${second}\n`);
  const responses = sessionResponseRows(sid);
  expect(responses).toHaveLength(2);
  expect(responses[0]).toEqual(original.responses[0]);
  expect(responses[1]!.input_ids).toEqual([pending.input_id]);
  expect(sessionInputRows(sid).map((row) => ({ id: row.input_id, state: row.state, response: row.response_id }))).toEqual([
    { id: input.input_id, state: 'admitted', response: reply.id },
    { id: pending.input_id, state: 'admitted', response: next.id },
  ]);
  expect(sessionOutputRows(sid, reply.id)).toEqual(original.output.filter((row: { response_id: string }) => row.response_id === reply.id));
  let native = nativeTranscriptRows(sid);
  await expect.poll(() => {
    native = nativeTranscriptRows(sid);
    return native.filter((row) => !row.subpath).map((row) => JSON.parse(row.entry_json))
      .flatMap((entry) => entry.type === 'user' && Array.isArray(entry.message?.content)
        ? entry.message.content.filter((block: Record<string, unknown>) => block.type === 'tool_result') : []).length;
  }, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  expect(native.slice(0, original.native.length)).toEqual(original.native);
  const entries = native.filter((row) => !row.subpath).map((row) => JSON.parse(row.entry_json));
  for (const prompt of [firstPrompt, secondPrompt]) {
    expect(entries.filter((entry) => entry.type === 'user' && (entry.message?.content === prompt
      || entry.message?.content?.some?.((block: Record<string, unknown>) => block.type === 'text' && block.text === prompt)))).toHaveLength(1);
  }
  const calls = entries.flatMap((entry) => entry.type === 'assistant' && Array.isArray(entry.message?.content)
    ? entry.message.content.filter((block: Record<string, any>) => block.type === 'tool_use' && block.input?.command?.includes(path)) : []);
  expect(calls).toHaveLength(2);
  const results = entries.flatMap((entry) => entry.type === 'user' && Array.isArray(entry.message?.content)
    ? entry.message.content.filter((block: Record<string, any>) => block.type === 'tool_result') : []);
  for (const call of calls) expect(results.filter((result) => result.tool_use_id === call.id)).toHaveLength(1);
  for (const cold of [false, true]) {
    if (cold) await page.reload({ waitUntil: 'domcontentloaded' });
    for (const message of [reply, next]) await expect(page.locator(`[data-message-id="${message.id}"]`)).toContainText(message === reply ? first : second);
    for (const message of [reply, next]) {
      const turn = page.locator(`[data-message-id="${message.id}"]`);
      await expect(turn.getByTestId('assistant-turn-process-trigger')).toBeAttached();
      await revealAssistantProcess(page, { within: turn });
    }
    for (const call of calls) {
      const card = page.locator(`[data-tool-call-id="${call.id}"]`);
      await expect(card).toHaveCount(1);
      await card.getByRole('button').first().click();
      await expect(card.locator('pre').first()).toBeVisible();
      expect(JSON.parse((await card.locator('pre').first().textContent())!)).toEqual(call.input);
      const nativeResult = results.find((result) => result.tool_use_id === call.id);
      const output = typeof nativeResult.content === 'string' ? nativeResult.content
        : nativeResult.content.map((block: { text?: string }) => block.text ?? '').join('');
      const displayedOutput = card.locator('pre').nth(1);
      await displayedOutput.scrollIntoViewIfNeeded();
      await expect(displayedOutput).toBeVisible();
      expect((await displayedOutput.locator('code > span').allTextContents()).join('\n')).toBe(output.trim());
    }
  }
  await info.attach('same-live-runtime-recovered', { body: JSON.stringify({ sid, input, pending, responses,
    originalProcesses: initialProcesses, finalProcesses: processes(handle, uid), native, calls, results,
    history: await api.getMessages(sid, 50), output: sessionOutputRows(sid) }), contentType: 'application/json' });
  await page.screenshot({ path: info.outputPath('recovered-conversation.png'), fullPage: true });
});
