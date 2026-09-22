/** Real zero-model Pi dialogs, with supplier stdout and durable diagnostics as oracles. */
import { randomUUID } from 'node:crypto';
import { expect, test, type Page, type TestInfo } from '@playwright/test';

import { AstraApi, type PendingInteraction } from './astraApi';
import { framesForTurn, sessionEvents, snapshotDoc, waitForTurnTerminalProof } from './dbOracle';
import { engineProfiles } from './engineProfile';
import { object } from './piChildFailure';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from './sandboxOps';
import { openSessionView, sendPrompt } from './sessionPage';
import { aiStreamBodies, mirrorSseBodies } from './sseBodies';

export interface PiFormStep {
  method: 'select' | 'confirm' | 'input' | 'editor';
  options?: string[];
}

export class PiNativeFormScene {
  readonly marker = `PI_FORM_${randomUUID().replaceAll('-', '')}`;
  sessionId = '';
  agentId = '';
  turnId = '';
  resultPath = '';
  private reloadTurn = '';
  private handle?: SandboxHandle;
  private pipe?: string;
  private readonly observations: unknown[] = [];
  private readonly expectedResults: Record<string, unknown>[] = [];

  constructor(readonly api: AstraApi, readonly page: Page, readonly steps: PiFormStep[]) {}

  title(index: number): string { return `${this.marker}:${index}`; }

  private pipeFor(turnId: string): string | null {
    const anchors = [
      object(snapshotDoc(this.sessionId)?.current_turn_engine_anchor).engine_turn_id,
      ...framesForTurn(turnId).map((frame) => frame.engine_turn_id),
      ...sessionEvents(this.sessionId).filter((event) => event.turn_id === turnId)
        .map((event) => object(event.payload).engine_turn_id),
    ].filter((value): value is string => typeof value === 'string' && value.startsWith('pi-rpc-v1.'));
    const pipes = [...new Set(anchors.map((anchor) => object(JSON.parse(
      Buffer.from(anchor.slice('pi-rpc-v1.'.length), 'base64url').toString(),
    )).pty_session_id))];
    if (pipes.length === 0) return null;
    expect(pipes).toHaveLength(1);
    expect(pipes[0]).toEqual(expect.any(String));
    expect(String(pipes[0]).trim()).not.toBe('');
    return String(pipes[0]);
  }

  /** Execd viewer replays supplier stdout without taking the platform's holder or sending RPC. */
  nativeOutput(): Record<string, unknown>[] {
    if (!this.handle || !this.pipe) throw new Error('native viewer requires the actual sandbox and Pi pipe');
    const script = [
      "python3 - <<'PY'", 'import asyncio, json', 'from websockets.asyncio.client import connect',
      `url = ${JSON.stringify(`ws://127.0.0.1:44772/pty/${this.pipe}/ws?mode=viewer&since=0`)}`,
      'async def main():', '    buffer = b""', '    offsets = []', '    records = []',
      '    async with connect(url, max_size=64*1024*1024) as ws:',
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
    const result = object(JSON.parse(sandboxExec(this.handle, script, 15_000)));
    expect(result.finished_replay).toBe(true);
    expect(Array.isArray(result.records)).toBe(true);
    return (result.records as unknown[]).map(object);
  }

  async noFailure(phase: 'live' | 'idle' = 'live'): Promise<void> {
    const bodies = await aiStreamBodies(this.page);
    this.observations.push({ browserBodies: bodies });
    const frames = bodies.flatMap((body) => body.text.slice(0, body.text.lastIndexOf('\n') + 1)
      .split('\n').flatMap((line) => {
        if (!line.startsWith('data:')) return [];
        const data = line.slice(5).trim();
        return data && data !== '[DONE]' ? [object(JSON.parse(data))] : [];
      }));
    if (phase === 'live') expect(frames.length).toBeGreaterThan(0);
    expect(frames.filter((frame) => ['error', 'data-turn-failure'].includes(String(frame.type)))).toEqual([]);
    expect(frames.filter((frame) => String(frame.type).startsWith('tool-'))).toEqual([]);
    expect(sessionEvents(this.sessionId).filter((event) => event.event_type === 'turn.failed')).toEqual([]);
    await expect(this.page.getByTestId('run-view').getByText(
      /本轮执行失败|上一条消息已送达沙箱，但这一轮执行失败了|This turn failed/i,
    )).toHaveCount(0);
  }

  private async completed(previous: string | null | undefined): Promise<string> {
    const ended = await this.api.waitForSession(this.sessionId, (value) => {
      if (value.last_turn_status === 'FAILED' || value.last_error) throw new Error(JSON.stringify(value));
      return Boolean(value.last_turn_id && value.last_turn_id !== previous
        && value.last_turn_status === 'COMPLETED' && value.state === 'READY'
        && !value.current_turn_id && !value.pending_interaction);
    }, 30_000);
    const turnId = String(ended.last_turn_id);
    await waitForTurnTerminalProof(this.sessionId, turnId, 'COMPLETED', 5_000);
    return turnId;
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
      environment_name: environment!.name, prewarm_enabled: false,
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
    const previous = (await this.api.getSession(this.sessionId)).last_turn_id;
    await sendPrompt(this.page, this.sessionId, '/reload-runtime');
    this.reloadTurn = await this.completed(previous);
    await expect.poll(() => this.pipeFor(this.reloadTurn), { timeout: 15_000 }).not.toBeNull();
    this.pipe = this.pipeFor(this.reloadTurn)!;
    const wire = this.nativeOutput();
    expect(wire.filter((row) => row.type === 'response' && row.command === 'get_state')
      .some((row) => row.success === true && object(row.data).sessionId === nativeSessionId)).toBe(true);
    const acks = wire.filter((row) => row.type === 'response' && row.command === 'prompt');
    expect(acks).toHaveLength(1);
    expect(acks[0]).toMatchObject({ success: true });
    this.observations.push({ reloadWire: wire });
    await this.noFailure();
    await sendPrompt(this.page, this.sessionId, '/astrabox-form-semantics');
  }

  async question(index: number): Promise<PendingInteraction> {
    const waiting = await this.api.waitForSession(this.sessionId, (value) => {
      if (value.last_turn_status === 'FAILED' || value.last_error) throw new Error(JSON.stringify(value));
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
    if (index === 0) this.turnId = String(pending.turn_id);
    expect(this.turnId).not.toBe('');
    expect(pending.turn_id).toBe(this.turnId);
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
      .toMatchObject({ interaction_id: raw.id, turn_id: this.turnId });
    expect(JSON.parse(await this.api.downloadFileText(this.sessionId, this.resultPath, 10_000)))
      .toEqual(this.expectedResults);
    return pending;
  }

  async submit(pending: PendingInteraction, field: 'option_label' | 'free_text', exactValue: string): Promise<void> {
    const button = this.page.getByTestId('pending-interaction-panel')
      .getByRole('button', { name: /Submit answer|提交回答/ });
    expect(await button.count(), 'the native answer, including an empty string, must offer Submit rather than Decline').toBe(1);
    await expect(button).toBeEnabled();
    const response = this.page.waitForResponse((value) => value.request().method() === 'POST'
      && value.url().includes(`/sessions/${this.sessionId}/interaction-respond`));
    await button.click();
    const received = await response;
    const sent = object(received.request().postDataJSON());
    this.observations.push({ submitted: sent, status: received.status() });
    expect(received.status()).toBe(200);
    expect(sent.interaction_id).toBe(pending.interaction_id);
    const answer = object(sent.answer);
    expect(answer.decline).not.toBe(true);
    expect(answer.answers).toEqual([{ question_id: pending.interaction_id, [field]: exactValue }]);
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
    expect(await this.completed(this.reloadTurn)).toBe(this.turnId);
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
    // Zero-model Pi defers its SessionStore file. The real entry_appended events
    // belong in engine.diagnostic/sessionEvents, not framesForTurn or an invented file.
    const events = sessionEvents(this.sessionId).filter((event) => event.turn_id === this.turnId
      && event.event_type === 'engine.diagnostic' && object(event.payload).event_type === 'pi.rpc');
    expect(events.filter((event) => object(event.payload).subtype === 'entry_appended')
      .map((event) => object(event.payload).raw)).toEqual(appended);
    const ackEvents = events.filter((event) => object(event.payload).subtype === 'prompt'
      && object(object(event.payload).raw).id === acks[1]!.id);
    expect(ackEvents).toHaveLength(1);
    expect(object(ackEvents[0]!.payload).raw).toEqual(acks[1]);
    const finishes = framesForTurn(this.turnId).filter((event) => object(event.payload).type === 'finish'
      && object(event.payload).finishReason === 'stop');
    expect(finishes).toHaveLength(1);
    expect(Number(finishes[0]!.event_seq)).toBeGreaterThan(Number(ackEvents[0]!.event_seq));
    const history = await this.api.getMessages(this.sessionId, 100);
    expect(history.messages.flatMap((message) => message.blocks ?? [])
      .filter((block) => ['tool_use', 'tool_result'].includes(String(block.type)))).toEqual([]);
    this.observations.push({ finalWire: wire, events });
    await expect(this.page.getByTestId('pending-interaction-panel')).toHaveCount(0);
    await this.noFailure();
    await this.page.reload({ waitUntil: 'domcontentloaded' });
    await expect(this.page.getByTestId('run-view')).toBeVisible();
    await expect(this.page.getByTestId('pending-interaction-panel')).toHaveCount(0);
    expect((await this.api.getSession(this.sessionId)).last_turn_status).toBe('COMPLETED');
    expect(JSON.parse(await this.api.downloadFileText(this.sessionId, this.resultPath, 10_000))).toEqual(this.expectedResults);
    await this.noFailure('idle');
  }

  async attachFailure(info: TestInfo): Promise<void> {
    const reads = await Promise.allSettled([
      this.sessionId ? this.api.getSession(this.sessionId) : Promise.resolve(null),
      this.sessionId ? this.api.getMessages(this.sessionId, 100) : Promise.resolve(null),
      aiStreamBodies(this.page),
      Promise.resolve().then(() => this.sessionId ? sessionEvents(this.sessionId) : []),
      Promise.resolve().then(() => this.handle && this.pipe ? this.nativeOutput() : []),
      this.resultPath ? this.api.downloadFileText(this.sessionId, this.resultPath, 10_000) : Promise.resolve(null),
    ]);
    await info.attach('pi-form-semantics-scene', {
      body: JSON.stringify({ sessionId: this.sessionId, agentId: this.agentId, marker: this.marker,
        steps: this.steps, observations: this.observations, reads }), contentType: 'application/json',
    });
  }
}
