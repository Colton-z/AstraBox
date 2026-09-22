/** Batch export preserves native SessionStore files after their compute is released. */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { requireSandboxHandle, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

interface NativeFile {
  path: string;
  sdkSessionId: string;
  subpath: string | null;
  entries: Record<string, unknown>[];
}

function nativeFiles(sessionId: string): NativeFile[] {
  const rows = documentsByField('transcript_entries', '$.platform_session_id', sessionId);
  const files = new Map<string, NativeFile>();
  rows.sort((a, b) => Number(a.seq) - Number(b.seq));
  for (const row of rows) {
    expect(Number.isSafeInteger(row.seq), 'native entries have an explicit append order').toBe(true);
    expect(typeof row.project_key).toBe('string');
    expect(typeof row.session_id).toBe('string');
    expect(typeof row.entry_json).toBe('string');
    const projectKey = String(row.project_key);
    const sdkSessionId = String(row.session_id);
    expect(projectKey).not.toBe('');
    expect(sdkSessionId).not.toBe('');
    const subpath = row.subpath == null ? null : String(row.subpath);
    const path = `${projectKey}/${subpath ?? sdkSessionId}.jsonl`;
    const file = files.get(path) ?? { path, sdkSessionId, subpath, entries: [] };
    expect(file.sdkSessionId, 'a filename must not merge different native sessions').toBe(sdkSessionId);
    file.entries.push(JSON.parse(String(row.entry_json)) as Record<string, unknown>);
    files.set(path, file);
  }
  return [...files.values()];
}

function hasConversation(files: NativeFile[], prompt: string): boolean {
  const mains = files.filter((file) => file.subpath === null);
  return mains.length === 1
    && mains[0].entries.some((entry) => entry.type === 'user' && JSON.stringify(entry).includes(prompt))
    && mains[0].entries.some((entry) => entry.type === 'assistant');
}

function tarText(archive: Buffer, args: string[]): string {
  return execFileSync('tar', args, {
    input: archive,
    encoding: 'utf8',
    timeout: 10_000,
    maxBuffer: 16 * 1024 * 1024,
  });
}

let agentId = '';
let emptyAgentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  await Promise.all([agentId, emptyAgentId].filter(Boolean).map((id) => api.deleteAgent(id)));
});

test('batch transcript export preserves each session native JSONL after compute release', async ({
  request,
}) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  // Conversation tenancy gives each Session compute that this test may release.
  const agent = await api.createColdTestAgent(`__e2e_batch_native_${runId}`);
  agentId = agent.agent_id;
  const conversations: Array<{ sessionId: string; marker: string; files: NativeFile[] }> = [];
  for (const index of [0, 1]) {
    const created = await api.startConversation(agentId);
    sessions.push(created.session_id);
    const ready = await api.waitForSessionReady(created.session_id);
    const sandboxId = String(ready.sandbox_id || '');
    expect(sandboxId).not.toBe('');
    const sandbox = await requireSandboxHandle(api, sandboxId);
    const marker = `BATCH_NATIVE_${runId}_${index}`;
    const prompt = `My note is ${marker}. Briefly explain why saving conversation history is useful. Do not use tools.`;
    const turn = await api.sendTurn(created.session_id, prompt);
    expect(turn.errorText, 'each exported conversation must have a successful real turn').toBeNull();
    expect(turn.text.trim(), 'each real turn must deliver assistant text').not.toBe('');
    await api.waitForSession(created.session_id, (session) => (
      session.state === 'READY'
      && !session.current_turn_id
      && session.last_turn_status === 'COMPLETED'
    ));
    await expect.poll(() => hasConversation(nativeFiles(created.session_id), prompt), {
      message: 'both native roles must reach the database before compute release',
    }).toBe(true);
    const reclaim = await api.terminateSandbox(created.session_id);
    expect(reclaim.session_id).toBe(created.session_id);
    expect(reclaim.status).toBe('sandbox-reclaimed');
    expect(reclaim.sandbox_id).toBe(sandboxId);
    expect(reclaim.killed, 'conversation-owned compute must be removed, not just detached').toBe(true);
    await waitForSandboxStopped(sandbox);
    const released = await api.getSession(created.session_id);
    expect(released.state).toBe('READY');
    expect(released.sandbox_id).toBeFalsy();
    const files = nativeFiles(created.session_id);
    expect(hasConversation(files, prompt), 'the native conversation must remain in database custody').toBe(true);
    conversations.push({ sessionId: created.session_id, marker, files });
  }

  const mainIds = conversations.map(({ files }) => files.find((file) => file.subpath === null)!.sdkSessionId);
  expect(new Set(mainIds).size, 'the archive must contain two distinct native conversations').toBe(2);
  const expectedFiles = conversations.flatMap(({ files }) => files);
  const paths = expectedFiles.map((file) => file.path);
  expect(new Set(paths).size, 'every native scope must own a distinct archive member').toBe(paths.length);

  const response = await request.get(apiPath(`/admin/sessions/transcripts?agent_id=${encodeURIComponent(agentId)}`));
  expect(response.status(), 'the existing filtered batch download must succeed').toBe(200);
  expect(response.headers()['content-type']).toContain('application/gzip');
  expect(response.headers()['content-disposition']).toContain('attachment');
  const archive = await response.body();
  expect(archive.length, 'HTTP success must not conceal an empty stream').toBeGreaterThan(0);
  const members = tarText(archive, ['--list', '--gzip', '--file=-']).trimEnd().split('\n');
  expect(members.sort(), 'agent filtering must export exactly these native scopes, once each')
    .toEqual([...paths].sort());

  // A second Agent has no conversations and needs no compute or model call.
  const emptyAgent = await api.createColdTestAgent(`__e2e_empty_export_${runId}`);
  emptyAgentId = emptyAgent.agent_id;
  const emptyResponse = await request.get(apiPath(`/admin/sessions/transcripts?agent_id=${encodeURIComponent(emptyAgentId)}`));
  expect(emptyResponse.status()).toBe(200);
  expect(emptyResponse.headers()['content-type']).toContain('application/gzip');
  const emptyArchive = await emptyResponse.body();
  expect(emptyArchive.length, 'an empty selection still returns a valid gzip archive').toBeGreaterThan(0);
  expect(tarText(emptyArchive, ['--list', '--gzip', '--file=-']),
    'a different Agent must not receive either native conversation').toBe('');

  for (const [index, conversation] of conversations.entries()) {
    const otherMarker = conversations[1 - index].marker;
    for (const file of conversation.files) {
      const jsonl = tarText(archive, ['--extract', '--gzip', '--to-stdout', '--file=-', '--no-wildcards', '--', file.path]);
      expect(jsonl.endsWith('\n'), 'every native record must end with a JSONL newline').toBe(true);
      const exported = jsonl.slice(0, -1).split('\n').map((line) => JSON.parse(line));
      expect(exported, `${file.path} must preserve every native field and append order`).toEqual(file.entries);
      expect(jsonl, 'a native file must not contain the other conversation').not.toContain(otherMarker);
      if (file.subpath === null) expect(jsonl).toContain(conversation.marker);
    }
    expect((await api.getSession(conversation.sessionId)).sandbox_id,
      'export must not provision replacement compute to read the durable Store').toBeFalsy();
  }
});
