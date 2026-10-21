/** Native peer discovery must retain credentials and stop before unauthorized work. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { agentTaskReferences, sessionRow } from '../fixtures/ingressRecords';
import { terminalOutput } from '../fixtures/nativeMcpServer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let assistantId = '';
let agentId = '';
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (assistantId) await api.deleteAssistant(assistantId);
  if (agentId) await api.deleteAgent(agentId);
});

test('native A2A rejects an unknown key and preserves Card authentication errors before a valid call', async ({ request }, info) => {
  const api = new AstraApi(request);
  const marker = `A2A_${randomUUID().replace(/-/g, '')}`;
  agentId = (await api.createColdTestAgent(`__e2e_peer_auth_${marker}`)).agent_id;
  const profile = await api.assistantEnvironmentName();
  assistantId = (await api.createAssistant({ display_name: `__e2e_peer_source_${marker}`,
    runtime_profile_name: profile,
    model_config_override: { model_name: await api.assistantModelName(profile) },
  })).assistant_id;
  const sourceId = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(sourceId);
  await api.waitForSessionReady(sourceId, 90_000);
  const home = String((await api.adminSessionDetail(sourceId)).runtime_identity?.config_dir || '');
  expect(home).toMatch(/^\/home\//);
  const key = `astrabox/${agentId}`;
  const invoke = async (positive: boolean) => {
    // Observe the pinned native tool's real HTTP, without replacing responses or credentials.
    const program = [
      'import json, os, urllib.error',
      `os.environ["HERMES_HOME"] = ${JSON.stringify(home)}`,
      'from plugins.platforms.a2a import tools',
      `key = ${JSON.stringify(key)}`,
      'peer = tools._configured_peers()[key]',
      'requests = []',
      'original = tools._http_json',
      'def observe(url, headers, timeout, method, data=None):',
      '    item = {"method": method, "url": url}',
      '    requests.append(item)',
      '    try:',
      '        result = original(url, headers, timeout, method, data)',
      '        item["status"] = 200',
      '        return result',
      '    except urllib.error.HTTPError as error:',
      '        item["status"] = error.code',
      '        raise',
      'tools._http_json = observe',
      ...(positive ? [
        `reply = tools.a2a_call({"agent": key, "message": ${JSON.stringify(
          `Write exactly ${marker} without a newline to ${marker}.txt in your own workspace using one foreground tool. `
          + 'Then report the file content and platform conversation link. Do not delegate or retry.',
        )}})`,
        'result = {"reply": reply, "requests": requests}',
      ] : [
        'listing = tools.a2a_list()',
        `unknown = tools.a2a_call({"agent": ${JSON.stringify(agentId)}, "message": "must not execute"})`,
        'unknown_requests = list(requests)',
        'requests.clear()',
        'unauthorized = [tools.a2a_call({"agent": peer["url"].rstrip("/") + suffix, "message": "must not execute"}) for suffix in ("", "/")]',
        'result = {"listing": listing, "unknown": unknown, "unknown_requests": unknown_requests, "unauthorized": unauthorized, "requests": requests}',
      ]),
      'print("E2E_PEER_JSON:" + json.dumps(result))',
    ].join('\n');
    const output = terminalOutput(await api.runTerminalCommand(sourceId,
      `/opt/hermes/.venv/bin/python - <<'PY'\n${program}\nPY`, '/tmp', positive ? 120_000 : 30_000));
    const records = output.split('\n').filter((line) => line.startsWith('E2E_PEER_JSON:'));
    expect(records, output).toHaveLength(1);
    return JSON.parse(records[0]!.slice('E2E_PEER_JSON:'.length));
  };
  const refused = await invoke(false);
  await info.attach('native-peer-refusals', { body: JSON.stringify(refused), contentType: 'application/json' });
  expect(refused.listing).toContain(`peer key: ${key}; discovery URL:`);
  expect(refused.listing).toContain(`a2a_call(agent="${key}", message=...)`);
  expect(refused.unknown).toContain(`unknown agent '${agentId}'`);
  expect(refused.unknown).toContain(key);
  expect(refused.unknown).toContain('exact configured peer key');
  expect(refused.unknown_requests).toEqual([]);
  expect(refused.unauthorized).toHaveLength(2);
  for (const error of refused.unauthorized) {
    expect(error).toContain('rejected auth (HTTP 401)');
    expect(error).not.toContain('405');
  }
  expect(refused.requests).toEqual([
    expect.objectContaining({ method: 'GET', status: 401, url: expect.stringContaining('/.well-known/agent-card.json') }),
    expect.objectContaining({ method: 'GET', status: 401, url: expect.stringContaining('/.well-known/agent-card.json') }),
  ]);
  expect(agentTaskReferences(agentId), 'refused calls must create neither target tasks nor their Sessions').toEqual([]);

  const accepted = await invoke(true);
  const tasks = agentTaskReferences(agentId).filter((row) => row.kind === 'task');
  expect(tasks).toHaveLength(1);
  const targetId = String(tasks[0]!.session_id || '');
  expect(targetId).not.toBe('');
  sessions.push(targetId);
  expect(sessionRow(targetId)).toMatchObject({ user_id: sessionRow(sourceId)?.user_id, agent_id: agentId, source_type: 'a2a' });
  expect(accepted.requests).toEqual([
    expect.objectContaining({ method: 'GET', status: 200 }),
    expect.objectContaining({ method: 'POST', status: 200, url: expect.stringMatching(/\/a2a$/) }),
  ]);
  expect(accepted.reply).toContain(marker);
  expect(accepted.reply).toContain(targetId);
  const messages = (await api.getMessages(targetId)).messages;
  expect(messages.filter((message) => message.role === 'user')).toHaveLength(1);
  expect(messages.filter((message) => message.role === 'assistant').map(messageText).join('\n')).toContain(marker);
  expect(await api.downloadFileText(targetId, `/workspace/${marker}.txt`)).toBe(marker);
  await info.attach('native-peer-accepted', { body: JSON.stringify({ sourceId, targetId, tasks, accepted, messages }), contentType: 'application/json' });
});
