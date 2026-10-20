/** A dead native CLI ends its response without a new input or fabricated tool result. */
import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { nativeTranscriptRows, sessionInputRows, sessionInteractionRows, sessionResponseRows } from '../fixtures/dbOracle';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();

function boundary(sid: string, action: 'arm' | 'read' | 'release' | 'clear', label = 'dead') {
  const program = `
import json,os,pathlib,sys
sid,action,label=sys.argv[1:]
assert os.environ.get('ASTRABOX_E2E_FAULTS')=='1'
p=pathlib.Path(os.environ['ASTRABOX_E2E_FAULT_FILE']+'.d')/('cli-loss-'+sid+'-'+label+'.json')
if action=='clear': p.unlink(missing_ok=True)
elif action in ('arm','release'):
 p.parent.mkdir(parents=True,exist_ok=True)
 value={'faults':{'hold_reader_boundary':1},'match':{'session_id':sid,'phase':'connected'},'release':False,'consumed':[]} if action=='arm' else {**json.loads(p.read_text()),'release':True}
 t=p.with_suffix('.tmp');t.write_text(json.dumps(value));t.chmod(0o644);t.replace(p)
print(p.read_text() if p.exists() else '{}')
`;
  return JSON.parse(execFileSync('docker', ['exec', requireServiceContainer(SERVER_CONTAINER_HANDLE),
    'python', '-c', program, sid, action, label], { encoding: 'utf8', timeout: 10_000 }));
}

function nativeProcess(handle: SandboxHandle, kill = false) {
  return JSON.parse(sandboxExec(handle, `python3 - <<'PY'
import json,os,pathlib,pwd,signal
account=pwd.getpwuid(1000)
if os.geteuid()!=account.pw_uid:
 os.setgid(account.pw_gid);os.setuid(account.pw_uid)
matches=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit(): continue
 try:
  if not os.path.samefile(p/'exe','/usr/local/bin/claude'): continue
  status=dict(line.split(':',1) for line in (p/'status').read_text().splitlines())
  pid=int(p.name);ppid=int(status['PPid']);uid=int(status['Uid'].split()[0])
  parent=pathlib.Path('/proc')/str(ppid)
  assert uid==1000 and b'sandbox_runner' in (parent/'cmdline').read_bytes()
  matches.append(dict(pid=pid,ppid=ppid,uid=uid,executable=os.readlink(p/'exe')))
 except (FileNotFoundError,ProcessLookupError,PermissionError): pass
if ${kill ? 'True' : 'False'}:
 assert len(matches)==1,matches
 os.kill(matches[0]['pid'],signal.SIGKILL)
print(json.dumps(matches))
PY`));
}

test('CLI-only loss during a question fails the same response and expires live and cold forms', async ({ page, request }, testInfo) => {
  const api = new AstraApi(request);
  const marker = `CLI_LOSS_${Date.now()}`;
  const agent = await api.defaultAgent();
  const { session_id: sid } = await api.startConversation(agent.agent_id);
  sessions.push(sid);
  const ready = await api.waitForSessionReady(sid);
  const handle = await requireSandboxHandle(api, String(ready.sandbox_id));
  await api.setPermissionMode(sid, 'default');
  await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  await openSessionView(page, sid);
  await sendPrompt(page, sid, `Call AskUserQuestion exactly once with two single-select questions: ${marker} first and ${marker} second, each offering YES and NO. Wait for both answers. Do not use any other tool.`);
  const question = await api.waitForPendingInteraction(sid);
  expect(question.tool_name).toBe('AskUserQuestion');
  const iid = question.interaction_id;
  const responseId = String(question.response_id);
  const toolId = String(question.tool_call_id);
  const before = nativeProcess(handle);
  expect(before).toHaveLength(1);
  const nativeBefore = nativeTranscriptRows(sid).filter((row) => !row.subpath);
  expect(nativeBefore.length).toBeGreaterThan(0);
  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toContainText(marker);

  // A live CLI survives reader replacement with the original question and response.
  boundary(sid, 'arm', 'live');
  try {
    await api.adminEvictRuntime(sid);
    await expect.poll(() => boundary(sid, 'read', 'live').consumed.length).toBe(1);
    expect(nativeProcess(handle)).toEqual(before);
    expect((await api.getSession(sid)).pending_interaction?.interaction_id).toBe(iid);
    boundary(sid, 'release', 'live');
    await api.setPermissionMode(sid, 'default');
    await expect.poll(() => sessionResponseRows(sid)[0]?.status).toBe('waiting');
    expect(sessionResponseRows(sid)[0].response_id).toBe(responseId);
  } finally {
    boundary(sid, 'clear', 'live');
  }

  // Hold the replacement reader before permission synchronization and journal replay.
  boundary(sid, 'arm');
  let killed: unknown;
  try {
    await api.adminEvictRuntime(sid);
    await expect.poll(() => boundary(sid, 'read').consumed.length).toBe(1);
    killed = nativeProcess(handle, true);
    expect(killed).toEqual(before);
    await expect.poll(() => nativeProcess(handle)).toEqual([]);
    expect(sandboxExec(handle, `kill -0 ${before[0].ppid}; printf alive`)).toBe('alive');
    // HTTP acceptance records durable ingress; it cannot resurrect the dead CLI.
    const questions = question.questions as Array<{ id: string }>;
    expect(questions).toHaveLength(2);
    const receipt = await api.answerPendingInteraction(sid, iid, {
      answers: questions.map((item) => ({ question_id: item.id, option_label: 'YES' })),
    });
    expect(receipt).toBeTruthy();
    boundary(sid, 'release');
    await api.waitForSession(sid, (detail) => !detail.pending_interaction
      && detail.runtime_status === 'idle' && detail.last_response?.response_id === responseId
      && detail.last_response.outcome === 'failed');
    await expect(panel).toHaveCount(0);
  } finally {
    boundary(sid, 'clear');
  }

  const responses = sessionResponseRows(sid);
  expect(responses).toHaveLength(1);
  expect(responses[0]).toMatchObject({ response_id: responseId, status: 'ended', outcome: 'failed', native_reason: 'ProcessError' });
  expect(responses[0].error).toMatchObject({ code: 'ProcessError' });
  expect(JSON.stringify(responses[0].error)).toContain('-9');
  const interactions = sessionInteractionRows(sid);
  expect(interactions).toHaveLength(1);
  expect(interactions[0]).toMatchObject({ interaction_id: iid, status: 'expired' });
  expect(interactions[0].closed_at).toBeTruthy();
  expect(sessionInputRows(sid)).toHaveLength(1);
  expect(nativeProcess(handle)).toEqual([]);
  expect(sandboxExec(handle, `kill -0 ${before[0].ppid}; printf alive`)).toBe('alive');
  expect((await api.getSession(sid)).sandbox_id).toBe(ready.sandbox_id);

  const message = page.getByTestId('assistant-message').filter({ hasText: marker }).last();
  const process = message.getByTestId('assistant-turn-process-trigger');
  if (await process.count() && await process.getAttribute('aria-expanded') === 'false') await process.click();
  for (const group of await message.getByTestId('assistant-process-trigger').all()) {
    if (await group.getAttribute('aria-expanded') === 'false') await group.click();
  }
  const card = page.locator(`[data-interaction-id="${iid}"]`);
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('data-interaction-status', 'expired');
  await expect(card.getByRole('radio').first()).toBeDisabled();
  await expect(card).toContainText('This questionnaire is closed and can no longer be submitted.');
  const history = await api.getMessages(sid, 100);
  const reply = history.messages.find((item) => item.id === responseId)!;
  const forms = reply.parts.filter((part) => part.type === 'data-interaction'
    && (part.data as Record<string, unknown>)?.interaction_id === iid);
  expect(forms).toHaveLength(1);
  expect(forms[0].data).toMatchObject({ status: 'expired', tool_call_id: toolId });
  expect(reply.metadata).toMatchObject({ outcome: 'failed', native_reason: 'ProcessError', error: { code: 'ProcessError' } });
  const nativeAfter = nativeTranscriptRows(sid).filter((row) => !row.subpath);
  expect(nativeAfter.slice(0, nativeBefore.length)).toEqual(nativeBefore);
  const blocks = nativeAfter.flatMap((row) => {
    const entry = JSON.parse(row.entry_json);
    return Array.isArray(entry.message?.content) ? entry.message.content as Array<Record<string, unknown>> : [];
  });
  expect(blocks.filter((part) => part.type === 'tool_use')).toEqual([expect.objectContaining({ name: 'AskUserQuestion', id: toolId })]);
  expect(blocks.filter((part) => part.type === 'tool_result')).toEqual([]);
  await testInfo.attach('cli-loss-native-live-cold-evidence', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
    sid, iid, responseId, toolId, before, killed, responses, interactions, history: reply, nativeBefore, nativeAfter,
  })) });
});
