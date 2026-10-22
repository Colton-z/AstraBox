import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral, sessionInputRows, sessionOutputRows } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});
const faultRoot = '/tmp/astrabox-hermes-protocol-exit';
type LiveNative = { id: string; session_key: string };
type NativeEvent = { type: string; seq: number; payload?: Record<string, unknown> };

function nativeRpc(handle: SandboxHandle, configDir: string, method: string, params: object): Record<string, unknown> {
  const program = `import asyncio,json,os,aiohttp
async def main():
 async with aiohttp.ClientSession() as client:
  async with client.ws_connect('http://127.0.0.1:9118/api/ws',headers={'X-AstraBox-Hermes-Token':os.environ['HERMES_DASHBOARD_SESSION_TOKEN']}) as ws:
   await ws.send_json({'jsonrpc':'2.0','id':'exit-evidence','method':${JSON.stringify(method)},'params':json.loads(${JSON.stringify(JSON.stringify(params))})})
   while True:
    reply=await ws.receive_json(timeout=15)
    if reply.get('id')=='exit-evidence':
     assert 'error' not in reply,reply
     print(json.dumps(reply['result']));return
asyncio.run(main())`;
  return JSON.parse(sandboxExec(handle, `source '${configDir}/astrabox-hermes.env'; "$HERMES_VENV/bin/python" - <<'PY'\n${program}\nPY`));
}

async function restartServer(request: AstraApi, replace: boolean) {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const docker = (args: string[]) => execFileSync('docker', args, { encoding: 'utf8', timeout: 40_000 }).trim();
  if (replace) {
    const image = process.env.ASTRABOX_E2E_REPAIRED_SERVER_IMAGE;
    const envFile = process.env.ASTRABOX_E2E_CANDIDATE_ENV_FILE;
    expect(image && envFile, 'the isolated runner must supply the repaired image and its own environment file').toBeTruthy();
    const before = JSON.parse(docker(['inspect', server]))[0];
    docker(['stop', '--time', '15', server]);
    docker(['rename', server, `${server}-prelude`]);
    const networks = Object.keys(before.NetworkSettings.Networks);
    const args = ['create', '--name', server, '--hostname', server, '--env-file', envFile!, '--network', networks[0]!];
    for (const [port, binds] of Object.entries(before.HostConfig.PortBindings) as Array<[string, Array<{ HostIp: string; HostPort: string }>]>) {
      for (const bind of binds) args.push('--publish', `${bind.HostIp ? `${bind.HostIp}:` : ''}${bind.HostPort}:${port}`);
    }
    for (const mount of before.Mounts) args.push('--mount', `type=${mount.Type},source=${mount.Type === 'volume' ? mount.Name : mount.Source},target=${mount.Destination}${mount.RW ? '' : ',readonly'}`);
    docker([...args, image!]);
    for (const network of networks.slice(1)) docker(['network', 'connect', network, server]);
    docker(['start', server]);
  } else docker(['restart', '--time', '15', server]);
  await expect.poll(async () => {
    try { await request.data('GET', '/user/current'); return true; } catch { return false; }
  }, { timeout: 45_000 }).toBe(true);
  expect(JSON.parse(docker(['inspect', server]))[0].Config.Image).toBe(process.env.ASTRABOX_E2E_REPAIRED_SERVER_IMAGE);
}

test('a real old-contract Assistant fault can end or archive after restart without losing its sibling or fabricating completion', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const profile = await api.assistantEnvironmentName();
  assistantId = (await api.createAssistant({ display_name: `__e2e_protocol_exit_${randomUUID()}`,
    runtime_profile_name: profile, model_config_override: { model_name: await api.assistantModelName(profile) },
  })).assistant_id;
  const target = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(target);
  await api.waitForSessionReady(target, 90_000);
  const detail = await api.adminSessionDetail(target);
  const box = String(detail.sandbox_id);
  const handle = await requireSandboxHandle(api, box);
  const config = String(detail.runtime_identity?.config_dir);
  expect(config).toMatch(/^\/home\//);
  sandboxExec(handle, `mkdir -p ${faultRoot}; chmod 777 ${faultRoot}`);
  const marker = randomUUID().replace(/-/g, '');
  const preservedFile = `/tmp/protocol-exit-${marker}`;
  const command = `printf ENTERED > ${faultRoot}/entered; while [ ! -f ${faultRoot}/release ]; do sleep 0.1; done; printf FIRST_${marker}`;
  const evidence: Record<string, unknown> = { target, assistantId, box };
  try {
    await api.data('POST', `/sessions/${target}/inputs`, { content: `Call terminal once with command ${JSON.stringify(command)}. Then repeat its output. No other tools or retries.` });
    await expect.poll(() => sandboxExec(handle, `cat ${faultRoot}/entered 2>/dev/null || true`), { timeout: 45_000 }).toBe('ENTERED');
    expect((await api.getSession(target)).runtime_status).toBe('working');
    await expect(api.endConversation(target)).rejects.toThrow(/SESSION_BUSY/);
    await expect(api.archiveSession(target)).rejects.toThrow(/SESSION_BUSY/);
    sandboxExec(handle, `touch ${faultRoot}/release`);
    const archive = (await api.startAssistantConversation(assistantId)).session_id;
    const sibling = (await api.startAssistantConversation(assistantId)).session_id;
    sessions.push(archive, sibling);
    for (const sid of [archive, sibling]) await api.waitForSessionReady(sid, 45_000);
    await api.data('POST', `/sessions/${archive}/inputs`, { content: `Call terminal once with command "printf ARCHIVE_${marker}" and repeat the result. No retries or other tools.` });
    await api.data('POST', `/sessions/${sibling}/inputs`, { content: `Call terminal once with command ${JSON.stringify(`printf SIBLING_${marker} > ${preservedFile}; cat ${preservedFile}`)} and repeat the result. No retries or other tools.` });
    const nativeBySession = new Map<string, LiveNative>();
    const nativeBefore: Record<string, unknown> = {};
    const live = () => nativeRpc(handle, config, 'session.active_list', {}).sessions as LiveNative[];
    for (const sid of [target, archive, sibling]) {
      await expect.poll(async () => {
        const key = (await api.adminSessionDetail(sid)).engine_session_key;
        const row = live().find((item) => item.session_key === key);
        if (!row) return false;
        nativeBySession.set(sid, row);
        const ring = nativeRpc(handle, config, 'session.events.since', { session_id: row.id, last_seen: 0 });
        nativeBefore[sid] = ring;
        return (ring.events as NativeEvent[]).some((event) => event.type === 'message.complete');
      }, { timeout: 45_000 }).toBe(true);
      const ring = nativeBefore[sid] as { events: NativeEvent[]; truncated: boolean };
      expect(ring.truncated).toBe(false);
      const completions = ring.events.filter((event) => event.type === 'tool.complete' && event.payload?.name === 'terminal');
      expect(completions).toHaveLength(1);
      expect(completions[0]!.payload).not.toHaveProperty('is_error');
      expect(sessionInputRows(sid)).toHaveLength(1);
    }
    expect(nativeRpc(handle, config, 'gateway.capabilities', {})).not.toHaveProperty('execution_tool_completion_is_error');
    evidence.nativeBefore = nativeBefore;
    await restartServer(api, true);
    for (const sid of [target, archive, sibling]) {
      await expect.poll(async () => (await api.adminSessionDetail(sid)).recovery_reason, { timeout: 30_000 }).toBe('runtime_protocol_incompatible');
      expect((await api.getSession(sid)).runtime_status).toBe('working');
    }
    // A second restart leaves blocked readers without any process-local gateway attachment.
    await restartServer(api, false);
    const siblingHistory = await api.getMessages(sibling);
    const beforeExit = live();
    // The isolated old-contract fixture uses the supplier's 600s orphan grace.
    // Missing targets cannot exercise native close or sibling preservation.
    for (const sid of [target, archive, sibling]) {
      expect(beforeExit.some((row) => row.id === nativeBySession.get(sid)!.id)).toBe(true);
    }
    evidence.nativeBeforeExit = beforeExit;
    const siblingNative = beforeExit.find((row) => row.id === nativeBySession.get(sibling)!.id)!;
    const inputBefore = sessionInputRows(target);
    await expect(api.data('POST', `/sessions/${target}/inputs`, { content: 'must be refused' })).rejects.toThrow(/RUNTIME_PROTOCOL_INCOMPATIBLE/);
    expect(sessionInputRows(target)).toEqual(inputBefore);
    await openSessionView(page, target);
    sandboxExec(handle, `touch ${faultRoot}/lose-close-ack`);
    const failed = page.waitForResponse((reply) => new URL(reply.url()).pathname === apiPath(`/sessions/${target}/conversation/end`));
    await page.getByRole('button', { name: 'End conversation', exact: true }).click();
    const refusal = await failed;
    expect(refusal.status()).toBe(502);
    expect((await refusal.json()).code).toBe('SESSION_PROCESS_CLEANUP_FAILED');
    expect(live().some((row) => row.id === nativeBySession.get(target)!.id)).toBe(false);
    expect((await api.adminSessionDetail(target)).state).not.toBe('TERMINATED');
    expect((await api.getSession(target)).current_responses).not.toHaveLength(0);
    sandboxExec(handle, `rm ${faultRoot}/lose-close-ack`);
    await page.getByRole('button', { name: 'End conversation', exact: true }).click();
    await expect.poll(async () => (await api.getSession(target)).state).toBe('TERMINATED');
    await api.endConversation(target);
    await api.archiveSession(archive);
    await api.archiveSession(archive);
    for (const sid of [target, archive]) {
      await expect.poll(async () => (await api.getSession(sid)).last_response?.native_reason).toBe('conversation_ended');
      expect((await api.getSession(sid)).last_response?.outcome).toBe('failed');
      expect(sessionInputRows(sid)).toHaveLength(1);
      expect(sessionOutputRows(sid).filter((row) => String(row.part.type).startsWith('tool-output-'))).toEqual([]);
      const cold = await api.getMessages(sid);
      expect(cold.messages.some((message) => message.role === 'assistant' && message.metadata?.outcome === 'failed')).toBe(true);
      expect(live().some((row) => row.id === nativeBySession.get(sid)!.id)).toBe(false);
    }
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('composer-prompt')).toBeDisabled();
    await expect(page.locator('[data-state="PROCESSING"]')).toHaveCount(0);
    expect(live()).toContainEqual(siblingNative);
    expect(await api.getMessages(sibling)).toEqual(siblingHistory);
    expect((await api.adminSessionDetail(sibling)).sandbox_id).toBe(box);
    expect(sandboxExec(handle, `cat ${preservedFile}`)).toBe(`SIBLING_${marker}`);
    expect(postgresRows<{ archived_at: string }>(`SELECT archived_at FROM sessions WHERE session_id=${sqlLiteral(archive)}`)[0]!.archived_at).toBeTruthy();
    evidence.nativeAfter = live();
    evidence.targetHistory = await api.getMessages(target);
    evidence.archivedHistory = await api.getMessages(archive);
    evidence.siblingHistory = siblingHistory;
    await info.attach('protocol-exit-cold-history', { body: await page.screenshot(), contentType: 'image/png' });
  } finally {
    await info.attach('protocol-exit-evidence', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
