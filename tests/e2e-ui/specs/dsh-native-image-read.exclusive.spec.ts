/** Real image pixels and native attachments survive loss of volume-free compute. */
import { execFileSync } from 'node:child_process';
import { randomBytes, randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { framesForTurn } from '../fixtures/dbOracle';
import { events } from '../fixtures/dshOutput';
import { engineProfileFor } from '../fixtures/engineProfile';
import { apiPath } from '../fixtures/env';
import { nativeRows } from '../fixtures/nativeChildLifecycle';
import { killSandbox, requireSandboxHandle, sandboxExec, waitForSandboxStopped,
  type SandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function imageBytes(handle: SandboxHandle, home: string, attachment: Record<string, unknown>, source: string) {
  const argumentsB64 = Buffer.from(JSON.stringify({ home, attachment, source })).toString('base64');
  return JSON.parse(sandboxExec(handle, `node --input-type=module <<'JS'\n`
    + "import { createHash } from 'node:crypto';\nimport { existsSync } from 'node:fs';\n"
    + "import { readImageFile } from '/opt/dsh/node_modules/@deepseek-ai/dsh-attachment-local/lib/index.js';\n"
    + `const { home, attachment, source } = JSON.parse(Buffer.from('${argumentsB64}', 'base64').toString());\n`
    + 'let result;\ntry {\n'
    + '  const stored = await readImageFile(`${home}/.dsh/attachments/v1`, attachment);\n'
    + "  result = { ok: true, sha256: createHash('sha256').update(stored.data).digest('hex'), bytes: stored.data.length, ref: stored.ref };\n"
    + '} catch (error) { result = { ok: false, code: error.code, message: error.message }; }\n'
    + 'console.log(JSON.stringify({ ...result, sourceExists: existsSync(source) }));\nJS'));
}

function attachmentCustody(handle: SandboxHandle, home: string, attachment: Record<string, unknown>) {
  const argumentsB64 = Buffer.from(JSON.stringify({ home, attachment })).toString('base64');
  return JSON.parse(sandboxExec(handle, `node --input-type=module <<'JS'\n`
    + "import { createHash, randomUUID } from 'node:crypto';\nimport { readFile } from 'node:fs/promises';\n"
    + `const { home, attachment } = JSON.parse(Buffer.from('${argumentsB64}', 'base64').toString());\n`
    + "const target = JSON.parse(await readFile(`${home}/.dsh/astrabox-runtime-state.json`, 'utf8'));\n"
    + "const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');\n"
    + 'const request = (operation, owner, fields = {}) => fetch(`${target.base_url}/${operation}`, {\n'
    + "  method: 'POST', headers: { 'Content-Type': 'application/json' },\n"
    + '  body: JSON.stringify({ owner, ...fields }), signal: AbortSignal.timeout(15_000),\n});\n'
    + "const owned = await request('load', target.owner);\n"
    + 'const envelope = await owned.json();\nconst snapshot = envelope.data;\n'
    + "const payload = Buffer.from(snapshot.payload_b64, 'base64');\n"
    + "const records = JSON.parse(payload.toString('utf8'));\n"
    + "const image = records.find((record) => record.kind === 'image' && record.ref.attachmentId === attachment.attachmentId);\n"
    + 'const foreign = [];\n'
    + "for (const [field, value] of Object.entries({ user_id: randomUUID(), subject_id: randomUUID(), subject_kind: 'agent', engine_kind: 'claude_code' })) {\n"
    + "  for (const operation of ['load', 'save']) {\n"
    + "    const response = await request(operation, { ...target.owner, [field]: value }, operation === 'save'\n"
    + "      ? { payload_b64: '', sha256: sha(Buffer.alloc(0)), expected_snapshot_id: null } : {});\n"
    + '    foreign.push({ field, operation, status: response.status });\n'
    + '  }\n}\n'
    + 'console.log(JSON.stringify({ owner: target.owner, status: owned.status, code: envelope.code,\n'
    + '  snapshotId: snapshot.snapshot_id, sha256: snapshot.sha256, actualSha256: sha(payload),\n'
    + '  size: snapshot.size, actualSize: payload.length, records: records.length,\n'
    + "  image: image ? { ref: image.ref, sha256: sha(Buffer.from(image.data, 'base64')) } : null, foreign }));\nJS"));
}

function noWorkspaceVolume(handle: SandboxHandle): void {
  expect(handle.runtime, 'the recovery proof inspects the real Kubernetes volumes').toBe('kubernetes');
  if (handle.runtime !== 'kubernetes') throw new Error('Kubernetes sandbox required');
  const pod = JSON.parse(execFileSync('kubectl', [
    'get', 'pod', handle.pod, '-n', handle.namespace, '-o', 'json',
  ], { encoding: 'utf8', timeout: 15_000 }));
  expect(pod.spec.volumes.filter((volume: Record<string, unknown>) => volume.persistentVolumeClaim),
    'native image custody must work without a persistent workspace volume').toEqual([]);
}

for (const route of [
  { title: 'native Files', model: undefined, provider: 'deepseek-official' },
  { title: 'a vision-capable gateway alias', model: 'openai-compatible/deepseek-flash', provider: 'astrabox-gateway' },
]) {
test(`DSH reads uploaded image pixels through ${route.title} and restores the image after sandbox loss`, async ({ request, page }) => {
  const api = new AstraApi(request);
  const profile = engineProfileFor('deepseek_harness');
  agentId = (await api.createAgent({ name: `__e2e_dsh_image_${randomUUID()}`,
    environment_name: profile.environment_name, model: route.model ?? profile.model, prewarm_enabled: false })).agent_id;
  const sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  await expect.poll(() => nativeRows(sessionId)
    .flatMap((row) => events(JSON.parse(String(row.entry_json))))
    .filter((event) => event.type === 'model/selection')
    .map((event) => ({ model: event.data.model, provider: event.data.provider })),
  ).toContainEqual({ model: route.model ?? profile.model, provider: route.provider });
  const detail = await api.adminSessionDetail(sessionId);
  const workspace = String(detail.runtime_identity?.workspace_dir || '').replace(/\/+$/, '');
  const sourceWorkspace = String(detail.runtime_identity?.workspace_source_dir || '').replace(/\/+$/, '');
  const home = String(detail.runtime_identity?.home_dir || '').replace(/\/+$/, '');
  expect(workspace).toMatch(/^\//);
  expect(sourceWorkspace).toMatch(/^\//);
  expect(home).toMatch(/^\//);
  await openSessionView(page, sessionId);

  // Only the pixels contain the answer. Neither the prompt, filename, nor PNG
  // metadata tells the model which random code to return.
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const marker = [...randomBytes(8)].map((byte) => alphabet[byte % alphabet.length]).join('');
  const png = await page.evaluate((code) => {
    const canvas = document.createElement('canvas');
    canvas.width = 960;
    canvas.height = 240;
    const context = canvas.getContext('2d')!;
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = '#000000';
    context.font = 'bold 96px monospace';
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillText(code, canvas.width / 2, canvas.height / 2);
    return canvas.toDataURL('image/png').split(',')[1]!;
  }, marker);
  const fileName = `${randomUUID()}.png`;
  const filePath = `${workspace}/${fileName}`;
  const upload = await request.fetch(apiPath(`/sessions/${sessionId}/files/upload`), {
    method: 'POST', multipart: { path: workspace,
      files: { name: fileName, mimeType: 'image/png', buffer: Buffer.from(png, 'base64') } },
    timeout: 60_000,
  });
  expect(upload.ok(), `image upload: ${upload.status()} ${await upload.text()}`).toBe(true);
  await test.info().attach('source-image', { body: Buffer.from(png, 'base64'), contentType: 'image/png' });
  await sendPrompt(page, sessionId,
    `Use read_image exactly once with file_path=${JSON.stringify(filePath)}. `
    + 'Read the code shown in the image and answer with that code. Do not use any other tools or delegate.');
  await api.waitForSession(sessionId, (session) => {
    expect(session.last_turn_status, 'the image turn must not fail').not.toBe('FAILED');
    return session.last_turn_status === 'COMPLETED' && session.state === 'READY' && !session.current_turn_id;
  });
  const messages = (await api.getMessages(sessionId)).messages;
  const answer = messages.find((message) => message.role === 'assistant' && messageText(message).includes(marker));
  expect(answer, 'the real model must read the code that exists only in the PNG pixels').toBeTruthy();

  const native = () => nativeRows(sessionId).flatMap((row) => events(JSON.parse(String(row.entry_json))));
  await expect.poll(() => native().filter((event) => event.type === 'turn/end')).toHaveLength(1);
  const source = native();
  const calls = source.filter((event) => event.type === 'tool/call');
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.data.name).toBe('read_image');
  expect(JSON.parse(String(call.data.arguments))).toEqual({ file_path: filePath });
  const results = source.filter((event) => event.type === 'tool/result')
    .map((event) => event.data.message as Record<string, unknown>);
  expect(results).toHaveLength(1);
  const result = results[0]!;
  expect(result.toolCallId).toBe(call.data.callId);
  expect(result.isError ?? false).toBe(false);
  const imageBlocks = (result.content as Record<string, unknown>[]).filter((block) => block.type === 'image');
  expect(imageBlocks).toHaveLength(1);
  const attachment = imageBlocks[0]!.attachment as Record<string, unknown>;
  expect(attachment).toMatchObject({ attachmentId: expect.stringMatching(/^sha256:[0-9a-f]{64}$/),
    mediaType: 'image/png', width: 960, height: 240 });
  expect(Number(attachment.bytes)).toBeGreaterThan(0);

  const originalHandle = await requireSandboxHandle(api, String(detail.sandbox_id));
  noWorkspaceVolume(originalHandle);
  // Box-level probes run outside the conversation's mount namespace.
  const originalBytes = imageBytes(originalHandle, home, attachment, `${sourceWorkspace}/${fileName}`);
  expect(originalBytes).toEqual({ ok: true, sha256: String(attachment.attachmentId).slice('sha256:'.length),
    bytes: attachment.bytes, ref: attachment, sourceExists: true });
  if (route.provider === 'astrabox-gateway') {
    const custody = attachmentCustody(originalHandle, home, attachment);
    await test.info().attach('native-attachment-custody', {
      body: JSON.stringify(custody), contentType: 'application/json',
    });
    expect(custody.owner).toMatchObject({ subject_kind: 'session', subject_id: sessionId, engine_kind: 'deepseek_harness' });
    expect(custody.owner.user_id).toEqual(expect.any(String));
    expect(custody.owner.user_id.length).toBeGreaterThan(0);
    expect(custody).toMatchObject({ status: 200, code: 'OK', records: 1,
      snapshotId: expect.stringMatching(/^[0-9a-f]{64}$/),
      sha256: custody.actualSha256, size: custody.actualSize,
      image: { ref: attachment, sha256: originalBytes.sha256 } });
    expect(custody.foreign).toHaveLength(8);
    for (const field of ['user_id', 'subject_id', 'subject_kind', 'engine_kind']) {
      for (const operation of ['load', 'save']) {
        expect(custody.foreign).toContainEqual({ field, operation, status: 403 });
      }
    }
  }

  let uploads: Record<string, unknown>[] | undefined;
  if (route.provider === 'deepseek-official') {
    // DeepSeek's provider uses Files; pi-ai sends the attachment inline.
    // Both routes must still read the code that exists only in the pixels.
    const handle = await requireSandboxHandle(api, String(detail.sandbox_id));
    const indexPath = Buffer.from(`${home}/.dsh/llm-deepseek/files-v3.json`).toString('base64');
    const index = JSON.parse(sandboxExec(handle, `python3 - <<'PY'\n`
      + `import base64\nfrom pathlib import Path\nprint(Path(base64.b64decode('${indexPath}').decode()).read_text())\nPY`));
    expect(index.formatVersion).toBe(3);
    uploads = (index.records as Record<string, unknown>[])
      .filter((record) => record.attachmentId === attachment.attachmentId);
    expect(uploads).toHaveLength(1);
    expect(uploads[0]!.fileId).toEqual(expect.any(String));
    expect(String(uploads[0]!.fileId).length).toBeGreaterThan(0);
    expect(Number(uploads[0]!.bytes)).toBeGreaterThan(0);
    expect(Number(uploads[0]!.expiresAt)).toBeGreaterThan(Number(uploads[0]!.createdAt));
  }

  const frames = framesForTurn(answer!.turn_id).map((row) => row.payload as Record<string, unknown>);
  expect(frames.filter((frame) => frame.type === 'tool-output-available')).toEqual([
    expect.objectContaining({ toolCallId: call.data.callId, output: { content: result.content, isError: false } }),
  ]);
  const retained = messages.find((message) => message.message_id === answer!.message_id)!;
  const toolResult = (retained.blocks ?? []).find((block) => block.type === 'tool_result');
  expect(toolResult).toMatchObject({ tool_use_id: call.data.callId, is_error: false, tool_result_state: 'output-available' });
  expect(String(toolResult!.content)).toContain(String(attachment.attachmentId));

  for (const reload of [false, true]) {
    if (reload) await page.reload();
    const reply = page.locator(`[data-message-id="${answer!.message_id}"]`);
    await expect(reply).toContainText(marker);
    await revealAssistantProcess(page, { within: reply });
    const card = reply.locator(`[data-tool-call-id="${String(call.data.callId)}"]`);
    await expect(card).toBeVisible();
    expect((await api.getMessages(sessionId)).messages.find((message) => message.message_id === answer!.message_id))
      .toEqual(retained);
  }

  // Destroy the substrate directly, so graceful platform shutdown cannot make
  // the test pass by taking a final snapshot. Only database custody survives.
  killSandbox(originalHandle);
  await waitForSandboxStopped(originalHandle);
  await api.recoverSession(sessionId);
  await api.waitForSessionReady(sessionId);
  const recoveredDetail = await api.adminSessionDetail(sessionId);
  expect(recoveredDetail.sandbox_id).toBeTruthy();
  expect(recoveredDetail.sandbox_id).not.toBe(detail.sandbox_id);
  const recoveredHome = String(recoveredDetail.runtime_identity?.home_dir || '').replace(/\/+$/, '');
  const recoveredSourceWorkspace = String(recoveredDetail.runtime_identity?.workspace_source_dir || '').replace(/\/+$/, '');
  expect(recoveredHome).toMatch(/^\//);
  expect(recoveredSourceWorkspace).toMatch(/^\//);
  const recoveredHandle = await requireSandboxHandle(api, String(recoveredDetail.sandbox_id));
  noWorkspaceVolume(recoveredHandle);
  const recoveredBytes = imageBytes(recoveredHandle, recoveredHome, attachment, `${recoveredSourceWorkspace}/${fileName}`);
  await test.info().attach('native-image-recovery', {
    body: JSON.stringify({ sessionId, originalBox: detail.sandbox_id, recoveredBox: recoveredDetail.sandbox_id,
      attachment, originalBytes, recoveredBytes }), contentType: 'application/json',
  });
  expect(recoveredBytes, 'the exact normalized image must be readable through the supplier API before the next input')
    .toEqual({ ...originalBytes, sourceExists: false });
  expect((await api.getMessages(sessionId)).messages.find((message) => message.message_id === answer!.message_id))
    .toEqual(retained);
  expect(native().filter((event) => event.type === 'tool/result').map((event) => event.data.message))
    .toEqual(results);
  await openSessionView(page, sessionId);
  await sendPrompt(page, sessionId,
    'Look at the image in the earlier read_image result again and repeat the code visible in its pixels. '
    + 'Use no tools, do not delegate, and do not create or upload any files.');
  const next = await api.waitForSession(sessionId, (session) => {
    expect(session.last_turn_status, 'the resumed image conversation must not fail').not.toBe('FAILED');
    return session.last_turn_id !== answer!.turn_id && session.last_turn_status === 'COMPLETED'
      && session.state === 'READY' && !session.current_turn_id;
  });
  const resumed = (await api.getMessages(sessionId)).messages
    .find((message) => message.role === 'assistant' && message.turn_id === next.last_turn_id);
  expect(resumed && messageText(resumed)).toContain(marker);
  await expect.poll(() => native().filter((event) => event.type === 'turn/end')).toHaveLength(2);
  expect(native().filter((event) => event.type === 'tool/call')).toEqual(calls);
  await page.reload();
  const firstReply = page.locator(`[data-message-id="${answer!.message_id}"]`);
  await expect(firstReply).toContainText(marker);
  await revealAssistantProcess(page, { within: firstReply });
  await expect(firstReply.locator(`[data-tool-call-id="${String(call.data.callId)}"]`)).toBeVisible();
  await expect(page.locator(`[data-message-id="${resumed!.message_id}"]`)).toContainText(marker);
  await test.info().attach('native-image-coverage', {
    body: JSON.stringify({ sessionId, turnId: answer!.turn_id, attachment, uploads }), contentType: 'application/json',
  });
});
}
