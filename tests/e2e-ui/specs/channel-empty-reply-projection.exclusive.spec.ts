/** Concurrent projection of a real empty native reply must not block its next delivery. */
import { spawn, execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import { sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { engineProfileFor } from '../fixtures/engineProfile';
import { apiPath, repoRoot } from '../fixtures/env';
import {
  armFrameHoldFault, clearFrameHoldFault, frameHoldConsumed, frameHoldFaultPath,
  releaseFrameHoldFault,
} from '../fixtures/frameHold';
import { channelOutboxRows, channelOutputSubscriptions } from '../fixtures/ingressRecords';
import { PlatformApi } from '../fixtures/platformApi';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (deploymentId) await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('an empty channel reply cannot block delivery when a stale projector resumes', async ({ request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const profile = engineProfileFor('claude_code');
  const id = randomUUID();
  const recipient = await channelCallback();
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const directory = `/tmp/astrabox-empty-reply-${id}`;
  const python = (program: string, ...args: string[]) => execFileSync('docker', [
    'exec', server, 'python', '-c', program, ...args,
  ], { encoding: 'utf8', timeout: 20_000 });
  const read = (name: string) => python(
    'import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.read_text() if p.exists() else "")',
    `${directory}/${name}`,
  ).trim();
  const release = (name: string) => python(
    'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', `${directory}/${name}`,
  );
  let hold = '';
  let replica: ReturnType<typeof spawn> | undefined;
  let replicaDone: Promise<number | null> | undefined;
  let replicaOutput = '';
  const evidence: Record<string, unknown> = {};
  try {
    const agent = await api.createColdTestAgent(`__e2e_empty_reply_${id}`);
    agentId = agent.agent_id;
    // Use the supplier's one-turn budget to avoid another model turn after
    // tool work. The assertions must prove a genuine empty native reply.
    await api.updateAgent(agentId, {
      ...agent, model: profile.model, engine_options: { sdk_options: { max_turns: 1 } },
    });
    const deployment = await platform.createDeployment(agentId, { scene: 'channel:generic_json', prompt_prefix: '' });
    deploymentId = deployment.deployment_id;
    const trigger = async (text: string) => {
      const response = await request.post(apiPath(`/deployments/${deploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(deployment.secret) },
        data: { text, message_id: randomUUID(), conversation_id: id, reply: { callback_url: recipient.url } },
      });
      expect(response.ok(), await response.text()).toBe(true);
      return (await response.json()).data as { session_id: string };
    };
    const baseline = `BASELINE_${id}`;
    const baselinePrompt = `Reply only ${baseline}. Do not use tools.`;
    const first = await trigger(baselinePrompt);
    const sessionId = first.session_id;
    sessions.push(sessionId);
    evidence.scene = { agentId, deploymentId, sessionId, directory };
    await api.waitForSessionReady(sessionId);
    expect((await api.adminSessionDetail(sessionId)).engine_kind).toBe('claude_code');
    expect(profile.modes.unattended).toBeTruthy();
    await api.setPermissionMode(sessionId, profile.modes.unattended!);
    await expect.poll(() => recipient.deliveries.map((row) => row.text), { timeout: 60_000 }).toEqual([baseline]);
    await expect.poll(() => channelOutputSubscriptions(sessionId)[0]?.after_seq)
      .toBe(sessionOutputRows(sessionId).at(-1)!.seq);

    python('import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir()', directory);
    replica = spawn('docker', ['exec', '-i', server, 'python', '-u', '-', sessionId, deploymentId, directory]);
    replicaDone = new Promise((resolve) => replica!.once('close', resolve));
    replica.on('error', (error) => { replicaOutput += String(error); });
    replica.stdout!.on('data', (part) => { replicaOutput += part.toString(); });
    replica.stderr!.on('data', (part) => { replicaOutput += part.toString(); });
    replica.stdin!.end(fs.readFileSync(path.join(repoRoot, 'tests/e2e-ui/fixtures/channel_projection_replica.py')));
    const running = () => {
      if (replica?.exitCode !== null) throw new Error(`projector exited: ${replicaOutput}`);
    };
    await expect.poll(() => { running(); return read('ready'); }).not.toBe('');

    hold = frameHoldFaultPath(sessionId);
    armFrameHoldFault(hold, sessionId, 'start');
    const marker = `TOOL_ONLY_${id}`;
    const filename = `empty-reply-${id}.txt`;
    const command = `printf '%s' '${marker}' > '${filename}'; cat '${filename}'`;
    const prompt = `Use Bash exactly once to execute exactly: ${command}\nDo not write any assistant text before or after the tool call.`;
    expect((await trigger(prompt)).session_id).toBe(sessionId);
    await expect.poll(() => frameHoldConsumed(hold, sessionId, 'start'), { timeout: 45_000 }).toBe(true);
    release('read');
    await expect.poll(() => { running(); return read('paused.json'); }).not.toBe('');
    const paused = JSON.parse(read('paused.json')) as {
      response_id: string; response_seq: number; output: Array<{ seq: number; part: { type: string } }>;
    };
    evidence.paused = paused;
    expect(paused.output.map((row) => row.part.type)).toEqual(['start']);
    await expect.poll(() => channelOutboxRows(sessionId).find((row) => row.response_id === paused.response_id))
      .toMatchObject({ state: 'PENDING', next_attempt_at: null });
    const original = channelOutboxRows(sessionId).find((row) => row.response_id === paused.response_id)!;
    evidence.original = original;

    releaseFrameHoldFault(hold);
    await expect.poll(() => sessionResponseRows(sessionId).find((row) => row.response_id === paused.response_id)?.status,
      { timeout: 60_000 }).toBe('ended');
    const rows = sessionOutputRows(sessionId, paused.response_id);
    evidence.emptyOutput = rows;
    expect(rows.at(-1)?.part.type).toBe('finish');
    expect(rows.some((row) => row.part.type === 'data-interaction')).toBe(false);
    expect(rows.filter((row) => row.part.type === 'text-delta')).toEqual([]);
    await expect.poll(() => channelOutputSubscriptions(sessionId)[0]?.after_seq).toBeGreaterThanOrEqual(rows.at(-1)!.seq);
    expect(await api.downloadFileText(sessionId, filename)).toBe(marker);
    const native = (await api.adminSessionTranscript(sessionId)).split('\n').filter((line) => line.trim())
      .map((line) => JSON.parse(line) as { type?: string; message?: { content?: unknown } });
    evidence.nativeBeforeResume = native;
    const inputs = native.filter((row) => row.type === 'user'
      && JSON.stringify(row.message).includes('Use Bash exactly once') && JSON.stringify(row.message).includes(marker));
    expect(inputs, 'the tool-only input must occur once in native custody').toHaveLength(1);
    const inputIndex = native.indexOf(inputs[0]);
    const segment = native.slice(inputIndex + 1);
    const blocks = (type: string) => segment.filter((row) => row.type === type)
      .flatMap((row) => Array.isArray(row.message?.content) ? row.message.content as Array<Record<string, unknown>> : []);
    expect(blocks('assistant').filter((block) => block.type === 'text')).toEqual([]);
    const calls = blocks('assistant').filter((block) => block.type === 'tool_use' && block.name === 'Bash');
    expect(calls).toHaveLength(1);
    expect(JSON.stringify(calls[0].input)).toContain(marker);
    const results = blocks('user').filter((block) => block.type === 'tool_result' && block.tool_use_id === calls[0].id);
    expect(results).toHaveLength(1);
    expect(results[0].is_error).not.toBe(true);
    expect(JSON.stringify(results[0].content)).toContain(marker);

    release('resume');
    expect(await replicaDone, replicaOutput).toBe(0);
    const retained = channelOutboxRows(sessionId).find((row) => row.response_id === paused.response_id);
    expect(retained).toMatchObject({ outbox_id: original.outbox_id, state: 'DELIVERED', next_attempt_at: null });
    expect(recipient.deliveries.map((row) => row.text)).toEqual([baseline]);
    const next = `NEXT_REPLY_${id}`;
    const nextPrompt = `Reply only ${next}. Do not use tools.`;
    expect((await trigger(nextPrompt)).session_id).toBe(sessionId);
    await expect.poll(() => recipient.deliveries.map((row) => row.text), { timeout: 60_000 }).toEqual([baseline, next]);
    await api.waitForSessionReady(sessionId);
    const history = await api.getMessages(sessionId, 50);
    expect(history.messages.filter((row) => row.role === 'user').map(messageText))
      .toEqual([baselinePrompt, prompt, nextPrompt]);
    expect(history.messages.filter((row) => row.role === 'assistant').map(messageText).filter(Boolean))
      .toEqual([baseline, next]);
    const finalNative = (await api.adminSessionTranscript(sessionId)).split('\n').filter((line) => line.trim())
      .map((line) => JSON.parse(line) as { type?: string; message?: { content?: unknown } });
    evidence.finalNative = finalNative;
    const nextInputs = finalNative.filter((row) => row.type === 'user' && JSON.stringify(row.message).includes(nextPrompt));
    expect(nextInputs, 'the next channel input must reach the native engine once').toHaveLength(1);
    const nextAnswers = finalNative.slice(finalNative.indexOf(nextInputs[0]) + 1)
      .filter((row) => row.type === 'assistant')
      .flatMap((row) => Array.isArray(row.message?.content) ? row.message.content as Array<Record<string, unknown>> : [])
      .filter((block) => block.type === 'text').map((block) => String(block.text ?? '')).join('');
    expect(nextAnswers).toBe(next);
    await expect.poll(() => channelOutboxRows(sessionId).map((row) => row.state))
      .toEqual(['DELIVERED', 'DELIVERED', 'DELIVERED']);
    expect(recipient.errors).toEqual([]);
    evidence.deliveries = recipient.deliveries;
    evidence.outboxes = channelOutboxRows(sessionId);
    evidence.replica = JSON.parse(read('created.json'));
  } finally {
    releaseFrameHoldFault(hold);
    if (replicaDone) {
      release('read');
      release('resume');
      await replicaDone;
    }
    clearFrameHoldFault(hold);
    await recipient.close();
    await test.info().attach('empty-reply-projection', {
      body: JSON.stringify({ ...evidence, replicaOutput, deliveries: recipient.deliveries, errors: recipient.errors }),
      contentType: 'application/json',
    });
  }
});
