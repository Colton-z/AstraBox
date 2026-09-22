/** A real 48 KiB HTTP MCP result reaches the model only as Claude's own persisted-output wrapper, live. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { framesForTurn } from '../fixtures/dbOracle';
import {
  browserStreamFrames, configureNativeMcpClientAgent, discoverNativeMcpTools, nativeRootEntries,
  object, serverLog, startNativeMcpServer, trackNativeMcpScene,
} from '../fixtures/nativeMcpServer';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { mirrorSseBodies } from '../fixtures/sseBodies';

const SERVER = 'e2e_persist';
const TOOL = `mcp__${SERVER}__large_output`;
/** The donor's real result size: 48 KiB of text, far above the tool's own limit. */
const OUTPUT_SIZE = 49_152;
/** The donor's per-tool `_meta["anthropic/maxResultSizeChars"]`, carried by the fixture. */
const MAX_RESULT_SIZE_CHARS = 4_096;
/** The donor's floor for "above Claude Code's persist-to-disk threshold". */
const PERSIST_FLOOR_CHARS = 40 * 1024;
/** The donor's retired platform truncation text; it must appear nowhere. */
const RETIRED_PLATFORM_TRUNCATION = 'tool result truncated for live display';
const OUTPUT_FRAME_TYPES = new Set(['tool-output-available', 'tool-output-error', 'tool-output-denied']);
const scene = trackNativeMcpScene('native-mcp-persisted-output-scene');

/**
 * The text of a native tool result, whether the SDK carried it as a string or
 * as text blocks. Durable history stores the same projection: a string, or the
 * text blocks joined by newlines.
 */
function supplierText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map((item) => {
      const block = object(item);
      expect(block.type, 'a native tool_result content item is a text block').toBe('text');
      return String(block.text);
    }).join('\n');
  }
  throw new Error(`native tool_result content carries no text: ${JSON.stringify(content)}`);
}

function turnToolBlocks(messages: Array<Record<string, unknown>>, turnId: string, type: string): Record<string, unknown>[] {
  return messages.filter((message) => message.turn_id === turnId)
    .flatMap((message) => (Array.isArray(message.blocks) ? message.blocks.map(object) : []))
    .filter((block) => block.type === type);
}

test('native HTTP MCP persisted output keeps the supplier wrapper across live, native and durable reads', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const headMarker = `E2E_MCP_PERSIST_HEAD_${runId}`;
  const tailMarker = `E2E_MCP_PERSIST_TAIL_${runId}`;
  const doneMarker = `DONE_E2E_MCP_PERSIST_${runId}`;
  const input = { head_marker: headMarker, tail_marker: tailMarker, output_size: OUTPUT_SIZE };
  const extraHeader = { 'X-Astrabox-E2E-Extra': `hdr-${randomUUID()}` };

  // The listener exists before the client Agent is configured, so the native
  // CLI discovers it at conversation bootstrap without eviction or retry.
  const started = await startNativeMcpServer(api, scene, { runId, tool: 'large_output' });
  const configured = await configureNativeMcpClientAgent(api, scene, {
    runId, label: 'persist', server: SERVER, headers: extraHeader,
  });
  const expectedServers = { [SERVER]: { type: 'http', url: scene.serverUrl, headers: extraHeader } };
  expect(configured.mcp_servers).toEqual(expectedServers);
  expect((await api.getAgent(configured.agent_id)).mcp_servers).toEqual(expectedServers);
  const sessionId = scene.clientSession;
  await api.setPermissionMode(sessionId, 'bypassPermissions');

  const discovery = await discoverNativeMcpTools(api, scene);
  expect(discovery, 'the client sandbox sees exactly the annotated tool over the real endpoint')
    .toEqual([{ name: 'large_output', meta: { 'anthropic/maxResultSizeChars': MAX_RESULT_SIZE_CHARS } }]);
  expect(await serverLog(api, scene), 'discovery must not execute the tool').toEqual([]);

  await page.setViewportSize({ width: 1280, height: 720 });
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  await sendPrompt(page, sessionId, [
    `E2E real MCP persisted-output ${runId}.`,
    `Call the MCP tool ${TOOL} exactly once.`,
    `Use arguments ${JSON.stringify(input)}.`,
    `Do not call any other tool. After the tool result returns, reply only ${doneMarker}.`,
  ].join('\n'));

  // The ordinary final answer reaches the original browser subscription.
  await expect(page.getByTestId('assistant-message').filter({ hasText: doneMarker }))
    .toHaveCount(1, { timeout: 120_000 });
  const settled = await api.waitForSession(sessionId, (session) => (
    !session.current_turn_id && session.state === 'READY' && Boolean(session.last_turn_id)
  ));
  expect(settled.last_error || null).toBeNull();
  expect(settled.last_turn_status).toBe('COMPLETED');
  const turnId = String(settled.last_turn_id);
  const status = page.getByTestId('run-view').locator('header').getByTestId('status-pill');
  await expect(status).toHaveText(/Ready|就绪/);

  // The server's own log: one real invocation with the requested markers, and
  // a response the fixture built, above the persist floor.
  const log = await serverLog(api, scene);
  expect(log).toHaveLength(1);
  expect(log[0]).toMatchObject({
    tool: 'large_output', phase: 'returned', head_marker: headMarker, tail_marker: tailMarker,
    output_size: OUTPUT_SIZE, output_chars: OUTPUT_SIZE, max_result_size_chars: MAX_RESULT_SIZE_CHARS,
  });
  expect(log[0].request_method).toBe('tools/call');
  expect(String(log[0].request_id || '')).not.toBe('');
  expect(log[0].request_headers, 'the Agent-configured extra header reaches the real MCP tool request')
    .toEqual({ 'x-astrabox-e2e-extra': extraHeader['X-Astrabox-E2E-Extra'] });
  await test.info().attach('native-mcp-extra-header-receipt', {
    body: JSON.stringify({ agentId: configured.agent_id, sessionId, serverSession: scene.serverSession,
      serverUrl: scene.serverUrl, configuredServers: expectedServers, receipt: log[0] }),
    contentType: 'application/json',
  });
  const response = String(log[0].response);
  expect(response).toHaveLength(OUTPUT_SIZE);
  expect(response.length).toBeGreaterThan(PERSIST_FLOOR_CHARS);
  expect(response.startsWith(`${headMarker}\n`)).toBe(true);
  expect(response.endsWith(`\n${tailMarker}`)).toBe(true);

  // Database-backed native SessionStore: the exact tool use, and the result
  // Claude Code wrote for it — its wrapper, not the fixture's response.
  const nativeBlocks = nativeRootEntries(sessionId).flatMap(({ entry }) => {
    if (entry.isSidechain === true || (entry.type !== 'assistant' && entry.type !== 'user')) return [];
    const content = object(entry.message).content;
    return Array.isArray(content) ? content.map((block) => ({ role: String(entry.type), block: object(block) })) : [];
  });
  const nativeUses = nativeBlocks.filter(({ block }) => block.type === 'tool_use');
  expect(nativeUses.map(({ role, block }) => ({ role, name: block.name, input: block.input })),
    'exactly one native tool use, the requested one, with the requested arguments')
    .toEqual([{ role: 'assistant', name: TOOL, input }]);
  const toolId = String(nativeUses[0].block.id);
  expect(toolId).not.toEqual('');
  const nativeResults = nativeBlocks.filter(({ block }) => block.type === 'tool_result');
  expect(nativeResults.map(({ role, block }) => ({ role, tool_use_id: block.tool_use_id })))
    .toEqual([{ role: 'user', tool_use_id: toolId }]);
  expect(nativeResults[0].block.is_error).not.toBe(true);
  const nativeContent = nativeResults[0].block.content;
  const wrapper = supplierText(nativeContent);
  expect(wrapper, 'Claude Code persisted the result and left its own wrapper').toContain('<persisted-output>');
  expect(wrapper).toContain('Full output saved to:');
  expect(wrapper, 'the wrapper previews the beginning of the result').toContain(headMarker);
  expect(wrapper, 'the wrapper does not carry the distant tail').not.toContain(tailMarker);
  expect(wrapper.length).toBeLessThan(OUTPUT_SIZE);
  expect(wrapper).not.toContain(RETIRED_PLATFORM_TRUNCATION);

  // Live: the frames the browser actually read carry the same tool ID and the
  // native content unchanged — no platform wrapper, text or metadata. The
  // mirrored copy of the body can trail the app's own read, so wait for this
  // tool's result to land in it before taking the snapshot that is asserted.
  const expectedOutputFrame = { type: 'tool-output-available', toolCallId: toolId, output: nativeContent };
  await expect.poll(async () => (await browserStreamFrames(page)).filter((frame) => (
    frame.type === 'tool-output-available' && frame.toolCallId === toolId
  )).length, { message: 'the mirrored browser stream must carry this tool result' }).toBeGreaterThan(0);
  const frames = await browserStreamFrames(page);
  expect(frames.filter((frame) => frame.type === 'unparsed')).toEqual([]);
  const liveInputs = frames.filter((frame) => frame.type === 'tool-input-available');
  expect(liveInputs.length).toBeGreaterThan(0);
  for (const frame of liveInputs) expect(frame).toMatchObject({ toolCallId: toolId, toolName: TOOL, input });
  const liveOutputs = frames.filter((frame) => OUTPUT_FRAME_TYPES.has(String(frame.type)));
  expect(liveOutputs.length, 'the browser received the tool result').toBeGreaterThan(0);
  for (const frame of liveOutputs) expect(frame).toEqual(expectedOutputFrame);
  expect(JSON.stringify(frames)).not.toContain(RETIRED_PLATFORM_TRUNCATION);
  const durableOutputs = framesForTurn(turnId).map((event) => object(event.payload))
    .filter((payload) => OUTPUT_FRAME_TYPES.has(String(payload.type)));
  // The journal also carries private routing metadata, not supplier output.
  // Preserve exact result cardinality, tool identity and full native content;
  // the browser frames above still admit no added public fields at all.
  expect(durableOutputs.map(({ type, toolCallId, output }) => ({ type, toolCallId, output })),
    'the durable frame ledger holds that one unmodified result once').toEqual([expectedOutputFrame]);
  // The settled turn folds its finished call behind one header, so the card is
  // reached the way a reader reaches it — by opening the fold first.
  await revealAssistantProcess(page);
  const card = page.getByTestId('assistant-message').getByRole('button', { name: new RegExp(`^${TOOL} (Done|已完成)`) });
  await expect(card).toHaveCount(1);

  // Durable public history: the same tool identity, the wrapper text and the
  // final answer, exactly once each.
  const history = await api.getMessages(sessionId);
  const historyUses = turnToolBlocks(history.messages, turnId, 'tool_use');
  expect(historyUses).toEqual([expect.objectContaining({ id: toolId, name: TOOL, input })]);
  const historyResults = turnToolBlocks(history.messages, turnId, 'tool_result');
  expect(historyResults).toEqual([expect.objectContaining({
    tool_use_id: toolId, is_error: false, tool_result_state: 'output-available', content: wrapper,
  })]);
  expect(history.messages.filter((message) => message.role === 'assistant'
    && messageText(message).includes(doneMarker))).toHaveLength(1);
  expect(JSON.stringify(history)).not.toContain(RETIRED_PLATFORM_TRUNCATION);

  // Cold: a reload rebuilds the same card and answer, and neither the settled
  // history nor the native Store was rewritten after the turn ended.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
  await expect(page.getByTestId('assistant-message').filter({ hasText: doneMarker }))
    .toHaveCount(1, { timeout: 45_000 });
  // A cold page holds the header alone until it is opened and its blocks fetched.
  await revealAssistantProcess(page);
  await expect(card).toHaveCount(1, { timeout: 30_000 });
  const cold = await api.getMessages(sessionId);
  expect(turnToolBlocks(cold.messages, turnId, 'tool_result')).toEqual(historyResults);
  expect(nativeRootEntries(sessionId).flatMap(({ entry }) => {
    const content = entry.type === 'user' ? object(entry.message).content : null;
    return Array.isArray(content) ? content.map(object).filter((block) => block.type === 'tool_result') : [];
  })).toEqual([expect.objectContaining({ tool_use_id: toolId, content: nativeContent })]);
  const coldServerLog = await serverLog(api, scene);
  expect(coldServerLog, 'the server was not asked again').toHaveLength(1);
  expect(coldServerLog, 'the original received header and tool receipt remain unchanged').toEqual(log);

  test.info().annotations.push({ type: 'native_mcp_persisted_output', description: JSON.stringify({
    sessionId, serverSession: scene.serverSession, serverUrl: scene.serverUrl, started, turnId, toolId,
    output_chars: response.length, wrapper_chars: wrapper.length, wrapper,
  }) });
});
