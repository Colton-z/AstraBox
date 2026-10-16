/** Real zero-model Pi dialogs, with supplier stdout and the platform's records as oracles. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page, type TestInfo } from '@playwright/test';

import { AstraApi, messageToolCalls, type PendingInteraction, type SessionRecord } from './astraApi';
import {
  sessionInputRows, sessionInteractionRows, sessionOutputRows, sessionResponseRows, type SessionInputRow,
} from './dbOracle';
import { engineProfiles } from './engineProfile';
import { apiPath } from './env';
import { currentPiPipe, object } from './piChildFailure';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from './sandboxOps';
import { openSessionView, sendPrompt } from './sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from './sseBodies';
import { runnerPortFor } from './staleWriterReconnect';

export interface PiFormStep {
  method: 'select' | 'confirm' | 'input' | 'editor';
  options?: string[];
}

/** Read Pi's own replay buffer without acquiring its command connection. */
export function piNativeOutput(
  handle: SandboxHandle, pipe: string, identity: Record<string, unknown> = {},
): Record<string, unknown>[] {
  const credentialPath = identity.isolated_session_id ? `${String(identity.home_dir)}/.astrabox-service-credential` : '';
  const port = identity.isolated_session_id ? runnerPortFor(identity) : 44772;
  const script = [
    "python3 - <<'PY'", 'import asyncio, json', 'from pathlib import Path', 'from websockets.asyncio.client import connect',
    `url = ${JSON.stringify(`ws://127.0.0.1:${port}/pty/${pipe}/ws?mode=viewer&since=0`)}`,
    `credential_path = ${JSON.stringify(credentialPath)}`,
    'headers = {"X-EXECD-ACCESS-TOKEN": Path(credential_path).read_text().strip()} if credential_path else {}',
    'async def main():', '    buffer = b""', '    offsets = []', '    records = []',
    '    async with connect(url, additional_headers=headers, max_size=64*1024*1024) as ws:',
    '        while True:', '            frame = await asyncio.wait_for(ws.recv(), 10)',
    '            if isinstance(frame, str):', '                control = json.loads(frame)',
    '                if control.get("type") == "connected":',
    '                    assert offsets and offsets[0] == 0, "native replay did not begin at zero"',
    '                    print(json.dumps({"finished_replay": True, "records": records}))',
    '                    return', '                continue',
    '            if frame[0] == 3:', '                offsets.append(int.from_bytes(frame[1:9], "big"))',
    '                buffer += frame[9:]', '            elif frame[0] == 1:', '                buffer += frame[1:]',
    '            else:', '                continue',
    '            while b"\\n" in buffer:', '                line, buffer = buffer.split(b"\\n", 1)',
    '                if line.strip(): records.append(json.loads(line))',
    'asyncio.run(main())', 'PY',
  ].join('\n');
  const result = object(JSON.parse(sandboxExec(handle, script, 15_000)));
  expect(result.finished_replay).toBe(true);
  expect(Array.isArray(result.records)).toBe(true);
  return (result.records as unknown[]).map(object);
}

export class PiNativeFormScene {
  readonly marker = `PI_FORM_${randomUUID().replaceAll('-', '')}`;
  sessionId = '';
  agentId = '';
  /** The response the command's dialogs pause, named by the first dialog. */
  responseId = '';
  resultPath = '';
  private handle?: SandboxHandle;
  private pipe?: string;
  private readonly observations: unknown[] = [];
  private readonly expectedResults: Record<string, unknown>[] = [];
  /** Each dialog Pi opened, and the answer the page sent for it, in order. */
  private readonly interactionIds: string[] = [];
  private readonly answers: Record<string, unknown>[] = [];

  constructor(readonly api: AstraApi, readonly page: Page, readonly steps: PiFormStep[]) {}

  title(index: number): string { return `${this.marker}:${index}`; }

  /** Execd viewer replays supplier stdout without taking the platform's holder or sending RPC. */
  nativeOutput(): Record<string, unknown>[] {
    if (!this.handle || !this.pipe) throw new Error('native viewer requires the actual sandbox and Pi pipe');
    return piNativeOutput(this.handle, this.pipe);
  }

  async noFailure(phase: 'live' | 'idle' = 'live'): Promise<void> {
    const bodies = await sessionStreamBodies(this.page);
    this.observations.push({ browserBodies: bodies });
    const frames = bodies.flatMap((body) => body.text.slice(0, body.text.lastIndexOf('\n') + 1)
      .split('\n').flatMap((line) => {
        if (!line.startsWith('data:')) return [];
        const data = line.slice(5).trim();
        return data && data !== '[DONE]' ? [object(JSON.parse(data))] : [];
      }));
    if (phase === 'live') expect(frames.length).toBeGreaterThan(0);
    expect(frames.filter((frame) => ['error', 'data-input-failed'].includes(String(frame.type)))).toEqual([]);
    expect(frames.filter((frame) => String(frame.type).startsWith('tool-'))).toEqual([]);
    expect(sessionResponseRows(this.sessionId).filter((row) => row.outcome === 'failed')).toEqual([]);
    expect(sessionInputRows(this.sessionId).filter((row) => row.state === 'failed')).toEqual([]);
    await expect(this.page.getByTestId('run-view').getByText(/本轮执行失败|This turn failed/i)).toHaveCount(0);
  }

  /**
   * Wait for Pi to take a command that writes no conversation, and return its input.
   *
   * Pi answers an extension command's prompt with a `handled` receipt once the
   * handler returns; the session's reader admits the input at that receipt
   * (`engine/pi_runtime_client.py`).
   */
  private async handled(content: string): Promise<SessionInputRow> {
    const inputs = () => sessionInputRows(this.sessionId).filter((row) => row.content === content);
    await expect.poll(() => inputs().map((row) => row.state), {
      timeout: 30_000, message: `Pi must take ${content} with its own receipt`,
    }).toEqual(['admitted']);
    await this.api.waitForSession(this.sessionId, (value) => {
      if (value.last_error) throw new Error(JSON.stringify(value));
      return value.state === 'READY' && !(value.current_responses ?? []).length && !value.pending_interaction;
    }, 30_000);
    const input = inputs()[0]!;
    expect(sessionResponseRows(this.sessionId).filter((row) => row.input_ids.includes(input.input_id)),
      'a command that writes no conversation is answered by no invented response').toEqual([]);
    return input;
  }

  /** Wait until the response the dialogs paused has ended and nothing runs or waits. */
  private async settled(): Promise<SessionRecord> {
    return this.api.waitForSession(this.sessionId, (value) => {
      if (value.last_response?.outcome === 'failed' || value.last_error) throw new Error(JSON.stringify(value));
      return value.state === 'READY' && !(value.current_responses ?? []).length && !value.pending_interaction
        && value.last_response?.response_id === this.responseId;
    }, 30_000);
  }

  async start(trackedSessions: string[]): Promise<void> {
    const profiles = engineProfiles().filter((profile) => profile.engine_kind === 'pi');
    expect(profiles).toHaveLength(1);
    const profile = profiles[0]!;
    const environments = await this.api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
    const environment = environments.find((item) => item.enabled === true
      && item.sandbox_tenancy === 'conversation' && item.engine_kind === 'pi'
      && item.runtime_template_name === profile.image);
    expect(environment, 'reuse the deployment-proven cold Pi Environment').toBeTruthy();
    const base = await this.api.getAgent(profile.agent_id);
    const options = object(base.engine_options);
    const settings = object(options.settings);
    const extensions = settings.extensions ?? [];
    expect(Array.isArray(extensions)).toBe(true);
    const reloadExtension = '/opt/pi/node_modules/@earendil-works/pi-coding-agent/examples/extensions/reload-runtime.ts';
    this.agentId = (await this.api.createAgent({
      name: `__e2e_${this.marker}`, model: base.model,
      environment_name: environment!.name, prepared_capacity: 0,
      engine_options: { ...options, settings: {
        ...settings, extensions: [...extensions as unknown[], reloadExtension],
      } },
    })).agent_id;
    this.sessionId = (await this.api.startConversation(this.agentId)).session_id;
    trackedSessions.push(this.sessionId);
    test.info().annotations.push({ type: 'e2e_session_id', description: this.sessionId });
    test.info().annotations.push({ type: 'engine_kind', description: 'pi' });
    await this.api.waitForSessionReady(this.sessionId);
    const detail = await this.api.adminSessionDetail(this.sessionId);
    const identity = object(detail.runtime_identity);
    expect(identity.sandbox_tenancy).toBe('conversation');
    const root = String(identity.workspace_dir ?? '').replace(/\/+$/, '');
    const home = String(identity.home_dir ?? '').replace(/\/+$/, '');
    const nativeSessionId = String(detail.engine_session_key ?? '').trim();
    expect(root).toMatch(/^\//);
    expect(home).toMatch(/^\//);
    expect(nativeSessionId).not.toBe('');
    expect(String(detail.sandbox_id ?? '')).not.toBe('');
    this.handle = await requireSandboxHandle(this.api, String(detail.sandbox_id));
    const directory = `${root}/.pi/extensions`;
    const extensionPath = `${directory}/astrabox-form-semantics.ts`;
    this.resultPath = `${root}/${this.marker}.json`;
    await this.api.data('POST', `/sessions/${this.sessionId}/files/mkdir`, { path: directory });
    await this.api.uploadFileText(this.sessionId, root, `${this.marker}.json`, '[]', 10_000);
    // Pi 0.85.1 rpc-mode.ts:127–138,235–250 defines literal strings, booleans,
    // and cancellation separately. The fixture records exactly what ctx.ui returns.
    const extension = [
      "import { writeFile } from 'node:fs/promises';", 'export default function (pi) {',
      "  pi.registerCommand('astrabox-form-semantics', {",
      "    description: 'Exercise native dialog answers without a model run',",
      '    handler: async (_args, ctx) => {', '      const results = [];',
      `      const steps = ${JSON.stringify(this.steps)};`,
      '      for (const [index, step] of steps.entries()) {',
      `        const title = ${JSON.stringify(this.marker)} + ':' + index;`,
      '        let value;',
      "        if (step.method === 'select') value = await ctx.ui.select(title, step.options);",
      "        else if (step.method === 'confirm') value = await ctx.ui.confirm(title, 'Apply this action?');",
      "        else if (step.method === 'input') value = await ctx.ui.input(title, 'Enter the exact value');",
      "        else value = await ctx.ui.editor(title);",
      `        const result = { marker: ${JSON.stringify(this.marker)}, index, method: step.method,`,
      '          value: value === undefined ? null : value, cancelled: value === undefined };',
      '        results.push(result);',
      "        pi.appendEntry('astrabox-form-result', result);",
      `        await writeFile(${JSON.stringify(this.resultPath)}, JSON.stringify(results));`,
      '      }', '    },', '  });', '}',
    ].join('\n');
    await this.api.uploadFileText(this.sessionId, directory, 'astrabox-form-semantics.ts', extension, 10_000);
    expect(await this.api.downloadFileText(this.sessionId, extensionPath, 10_000)).toBe(extension);
    sandboxExec(this.handle, [
      "python3 - <<'PY'", 'import json', 'from pathlib import Path',
      `path = Path(${JSON.stringify(`${home}/.pi/agent/settings.json`)})`,
      'settings = json.loads(path.read_text())',
      `assert ${JSON.stringify(reloadExtension)} in settings.get("extensions", [])`,
      `extension = ${JSON.stringify(extensionPath)}`,
      'extensions = settings.setdefault("extensions", [])', 'assert isinstance(extensions, list)',
      'assert extension not in extensions', 'extensions.append(extension)',
      'path.write_text(json.dumps(settings))',
      'assert extension in json.loads(path.read_text())["extensions"]', 'PY',
    ].join('\n'), 10_000);
    await mirrorSseBodies(this.page);
    await openSessionView(this.page, this.sessionId);
    await sendPrompt(this.page, this.sessionId, '/reload-runtime');
    const reload = await this.handled('/reload-runtime');
    await expect.poll(() => currentPiPipe(this.sessionId), { timeout: 15_000 }).not.toBeNull();
    this.pipe = currentPiPipe(this.sessionId)!;
    const wire = this.nativeOutput();
    expect(wire.filter((row) => row.type === 'response' && row.command === 'get_state')
      .some((row) => row.success === true && object(row.data).sessionId === nativeSessionId)).toBe(true);
    const acks = wire.filter((row) => row.type === 'response' && row.command === 'prompt');
    expect(acks, 'the receipt Pi wrote for the reload names its platform input').toEqual([expect.objectContaining({
      id: reload.input_id, success: true, data: expect.objectContaining({ disposition: 'handled' }),
    })]);
    this.observations.push({ reloadWire: wire });
    // The reload writes no conversation, so the page's stream has nothing to carry yet.
    await this.noFailure('idle');
    await sendPrompt(this.page, this.sessionId, '/astrabox-form-semantics');
  }

  async question(index: number): Promise<PendingInteraction> {
    const waiting = await this.api.waitForSession(this.sessionId, (value) => {
      if (value.last_response?.outcome === 'failed' || value.last_error) throw new Error(JSON.stringify(value));
      return object(value.pending_interaction?.raw_input).title === this.title(index);
    }, 15_000);
    const pending = waiting.pending_interaction!;
    const raw = object(pending.raw_input);
    expect(pending).toMatchObject({ presentation: 'form', tool_name: `pi.extension.${this.steps[index]!.method}` });
    expect(String(pending.tool_call_id ?? '')).toBe('');
    expect(raw).toMatchObject({ type: 'extension_ui_request', method: this.steps[index]!.method, title: this.title(index) });
    expect(raw.id).toEqual(expect.any(String));
    expect(String(raw.id).trim()).not.toBe('');
    expect(pending.interaction_id).toBe(raw.id);
    if (index === 0) this.responseId = String(pending.response_id ?? '');
    expect(this.responseId).not.toBe('');
    expect(pending.response_id, 'every dialog of one command pauses the same response').toBe(this.responseId);
    // The platform's records: the dialogs already answered, and this one open,
    // all on the response the command's handler runs in.
    expect(sessionInteractionRows(this.sessionId).map((row) => ({
      interaction_id: row.interaction_id, response_id: row.response_id, status: row.status,
    }))).toEqual([
      ...this.interactionIds.map((id) => ({ interaction_id: id, response_id: this.responseId, status: 'answered' })),
      { interaction_id: raw.id, response_id: this.responseId, status: 'open' },
    ]);
    this.interactionIds.push(String(raw.id));
    const wire = this.nativeOutput();
    expect(wire.filter((row) => row.type === 'extension_ui_request' && row.id === raw.id)).toEqual([raw]);
    expect(wire.filter((row) => row.type === 'response' && row.command === 'prompt')).toHaveLength(1);
    this.observations.push({ index, pending, wire });
    const panel = this.page.getByTestId('pending-interaction-panel');
    await expect(panel).toContainText(this.title(index));
    await this.noFailure();
    await this.page.reload({ waitUntil: 'domcontentloaded' });
    await expect(panel).toContainText(this.title(index));
    expect((await this.api.getPendingInteraction(this.sessionId))?.interaction_id).toBe(raw.id);
    expect(object((await this.api.getMessages(this.sessionId, 100)).pending_interaction))
      .toMatchObject({ interaction_id: raw.id, response_id: this.responseId });
    expect(JSON.parse(await this.api.downloadFileText(this.sessionId, this.resultPath, 10_000)))
      .toEqual(this.expectedResults);
    return pending;
  }

  async submit(pending: PendingInteraction, field: 'option_label' | 'free_text', exactValue: string): Promise<void> {
    const button = this.page.getByTestId('pending-interaction-panel')
      .getByRole('button', { name: /Submit answer|提交回答/ });
    expect(await button.count(), 'the native answer, including an empty string, must offer Submit rather than Decline').toBe(1);
    await expect(button).toBeEnabled();
    const answerPath = apiPath(
      `/sessions/${this.sessionId}/interactions/${encodeURIComponent(pending.interaction_id)}/answer`,
    );
    const response = this.page.waitForResponse((value) => value.request().method() === 'POST'
      && new URL(value.url()).pathname === answerPath);
    await button.click();
    const received = await response;
    const sent = object(received.request().postDataJSON());
    this.observations.push({ submitted: sent, status: received.status() });
    expect(received.status()).toBe(200);
    const answer = object(sent.answer);
    expect(answer.decline).not.toBe(true);
    expect(answer.answers).toEqual([{ question_id: pending.interaction_id, [field]: exactValue }]);
    this.answers.push(answer);
    const envelope = object(await received.json());
    expect(object(envelope.data ?? envelope)).toMatchObject({ interaction_id: pending.interaction_id, answered: true });
  }

  async result(index: number, value: string | boolean): Promise<void> {
    const expected = { marker: this.marker, index, method: this.steps[index]!.method, value, cancelled: false };
    this.expectedResults.push(expected);
    await expect.poll(async () => JSON.parse(await this.api.downloadFileText(this.sessionId, this.resultPath, 10_000)), {
      timeout: 15_000, message: 'the native handler must receive the exact value, not a cancellation or normalized text',
    }).toEqual(this.expectedResults);
    const wire = this.nativeOutput();
    const entries = wire.filter((row) => row.type === 'entry_appended'
      && object(row.entry).customType === 'astrabox-form-result');
    expect(entries.map((row) => object(row.entry).data)).toEqual(this.expectedResults);
    for (const row of entries) {
      expect(object(row.entry).id).toEqual(expect.any(String));
      expect(String(object(row.entry).id).trim()).not.toBe('');
    }
    this.observations.push({ index, expected, nativeResults: entries });
  }

  async finish(): Promise<void> {
    expect(this.expectedResults).toHaveLength(this.steps.length);
    const settled = await this.settled();
    expect(settled.last_response).toMatchObject({ status: 'ended', outcome: 'completed' });
    const wire = this.nativeOutput();
    const acks = wire.filter((row) => row.type === 'response' && row.command === 'prompt');
    expect(acks).toHaveLength(2);
    expect(acks[1]).toMatchObject({ success: true });
    expect(acks[1]!.id).not.toBe(acks[0]!.id);
    expect(wire.filter((row) => ['agent_start', 'agent_end', 'agent_settled',
      'tool_execution_start', 'tool_execution_end', 'extension_error'].includes(String(row.type)))).toEqual([]);
    const appended = wire.filter((row) => row.type === 'entry_appended'
      && object(row.entry).customType === 'astrabox-form-result');
    expect(appended.map((row) => object(row.entry).data)).toEqual(this.expectedResults);
    // The platform's records of the same command: Pi took the person's input
    // with its own receipt, the response its dialogs paused ended once with
    // Pi's word for that receipt, and each dialog closed with the answer sent.
    const disposition = object(acks[1]!.data).disposition;
    expect(disposition).toBe('handled');
    expect(sessionInputRows(this.sessionId).filter((row) => row.content === '/astrabox-form-semantics'))
      .toEqual([expect.objectContaining({ input_id: acks[1]!.id, state: 'admitted' })]);
    expect(sessionResponseRows(this.sessionId).filter((row) => row.response_id === this.responseId)).toEqual([
      expect.objectContaining({ status: 'ended', outcome: 'completed', native_reason: disposition }),
    ]);
    expect(sessionInteractionRows(this.sessionId)).toEqual(this.interactionIds.map((id, index) => expect.objectContaining({
      interaction_id: id, response_id: this.responseId, status: 'answered', answer: this.answers[index],
    })));
    const parts = sessionOutputRows(this.sessionId, this.responseId).map((row) => row.part);
    expect(parts.filter((part) => part.type === 'data-interaction').map((part) => object(part.data).interaction_id))
      .toEqual(this.interactionIds);
    expect(parts.filter((part) => part.type === 'finish').map((part) => part.finishReason),
      'the response pauses at each dialog and ends once, when the command returns')
      .toEqual([...this.interactionIds.map(() => 'tool-calls'), 'other']);
    expect(parts.filter((part) => part.type === 'error' || String(part.type).startsWith('tool-'))).toEqual([]);
    const history = await this.api.getMessages(this.sessionId, 100);
    expect(history.messages.flatMap(messageToolCalls), 'a native dialog is not shown as a tool call').toEqual([]);
    this.observations.push({ finalWire: wire, parts });
    await expect(this.page.getByTestId('pending-interaction-panel')).toHaveCount(0);
    await this.noFailure();
    await this.page.reload({ waitUntil: 'domcontentloaded' });
    await expect(this.page.getByTestId('run-view')).toBeVisible();
    await expect(this.page.getByTestId('pending-interaction-panel')).toHaveCount(0);
    expect((await this.api.getSession(this.sessionId)).last_response)
      .toMatchObject({ response_id: this.responseId, outcome: 'completed' });
    expect(JSON.parse(await this.api.downloadFileText(this.sessionId, this.resultPath, 10_000))).toEqual(this.expectedResults);
    await this.noFailure('idle');
  }

  async attachFailure(info: TestInfo): Promise<void> {
    const reads = await Promise.allSettled([
      this.sessionId ? this.api.getSession(this.sessionId) : Promise.resolve(null),
      this.sessionId ? this.api.getMessages(this.sessionId, 100) : Promise.resolve(null),
      sessionStreamBodies(this.page),
      Promise.resolve().then(() => this.sessionId ? sessionInputRows(this.sessionId) : []),
      Promise.resolve().then(() => this.sessionId ? sessionResponseRows(this.sessionId) : []),
      Promise.resolve().then(() => this.sessionId ? sessionInteractionRows(this.sessionId) : []),
      Promise.resolve().then(() => this.handle && this.pipe ? this.nativeOutput() : []),
      this.resultPath ? this.api.downloadFileText(this.sessionId, this.resultPath, 10_000) : Promise.resolve(null),
    ]);
    await info.attach('pi-form-semantics-scene', {
      body: JSON.stringify({ sessionId: this.sessionId, agentId: this.agentId, marker: this.marker,
        steps: this.steps, observations: this.observations, reads }), contentType: 'application/json',
    });
  }
}
