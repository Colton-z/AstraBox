/**
 * Real HTTP MCP fixture shared by the native MCP E2E cases.
 *
 * A separate conversation-tenancy sandbox owns the listener for its whole
 * lifetime, and the client Agent is configured only after that listener answers
 * on its exposed port, so native CLI discovery finds the server without an
 * eviction or readiness retry. The server process is started from a terminal of
 * a `createColdTestAgent()` conversation on purpose: a shared-tenancy terminal
 * is a disposable OpenSandbox isolation session that cannot keep a detached
 * listener alive past the command that started it.
 *
 * The fixture registers exactly one tool per server (`--tool`), so discovery
 * from the client sandbox names the tool its case is about and nothing else.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import type { Page } from '@playwright/test';
import { expect, test } from '@playwright/test';

import { AstraApi, type AgentRecord } from './astraApi';
import { documentsByField } from './dbOracle';
import { onPassOnly, trackSessions } from './sessionCleanup';
import { aiStreamBodies } from './sseBodies';

/** The image's own interpreter; the MCP Python SDK is installed for it. */
export const NATIVE_MCP_PYTHON = '/usr/local/bin/python3.12';
export const NATIVE_MCP_PORT = 5173;
export type NativeMcpTool = 'hang' | 'large_output';

export interface NativeMcpScene {
  /** Sessions kept on failure, deleted on pass (`trackSessions`). */
  sessions: string[];
  /** Throwaway Agents deleted on pass only. */
  agents: string[];
  serverSession: string;
  clientSession: string;
  serverLog: string;
  serverUrl: string;
}

export interface NativeMcpServerStart {
  pid: number;
  mcp_version: string;
}

export interface DiscoveredNativeMcpTool {
  name: string;
  meta: Record<string, unknown> | null;
}

export interface NativeRootEntry {
  seq: number;
  entry: Record<string, unknown>;
}

export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected an object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

/** Terminal HTTP success alone does not establish a successful command. */
export function terminalOutput(raw: string): string {
  const frames = raw.split('\n').filter((line) => line.startsWith('data:'))
    .map((line) => object(JSON.parse(line.slice(5).trim())));
  const output = frames.filter((frame) => frame.type === 'stdout' || frame.type === 'stderr')
    .map((frame) => String(frame.text || '')).join('');
  expect(frames.filter((frame) => frame.type === 'exit').map((frame) => frame.exit_code), output)
    .toEqual([0]);
  return output;
}

/** Run one Python program in the sandbox and return its single `E2E_MCP_JSON:` record. */
export async function pythonJson(api: AstraApi, sessionId: string, source: string): Promise<unknown> {
  const output = terminalOutput(await api.runTerminalCommand(
    sessionId, `${NATIVE_MCP_PYTHON} - <<'PY'\n${source}\nPY`, '/tmp', 30_000,
  ));
  const records = output.split('\n').filter((line) => line.startsWith('E2E_MCP_JSON:'));
  expect(records, output).toHaveLength(1);
  return JSON.parse(records[0].slice('E2E_MCP_JSON:'.length));
}

export async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return { available: true, value: await read() }; }
  catch (error) { return { available: false, error: String(error) }; }
}

/**
 * Parsed `data:` frames of every ai-stream body the browser has read so far.
 *
 * The mirror is a growing snapshot of each cloned body, so its last segment
 * may be a line still arriving: only complete lines are parsed. A complete
 * line that is not JSON is recorded as `unparsed` for the caller to reject.
 */
export async function browserStreamFrames(page: Page): Promise<Record<string, unknown>[]> {
  const frames: Record<string, unknown>[] = [];
  for (const body of await aiStreamBodies(page)) {
    for (const line of body.text.split(/\r?\n/).slice(0, -1)) {
      if (!line.startsWith('data:')) continue;
      const raw = line.slice(5).trim();
      if (!raw || raw === '[DONE]') continue;
      try { frames.push(object(JSON.parse(raw))); }
      catch { frames.push({ type: 'unparsed', line: raw.slice(0, 200) }); }
    }
  }
  return frames;
}

/** The root (non-subagent) native SessionStore entries of a session, in mirror order. */
export function nativeRootEntries(sessionId: string): NativeRootEntry[] {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({ seq: Number(row.seq), entry: object(JSON.parse(String(row.entry_json))) }))
    .sort((a, b) => a.seq - b.seq);
}

export async function serverLog(api: AstraApi, scene: NativeMcpScene): Promise<Record<string, unknown>[]> {
  const text = await api.downloadFileText(scene.serverSession, scene.serverLog);
  return text.split('\n').filter((line) => line.trim()).map((line) => object(JSON.parse(line)));
}

/**
 * Register the scene's retention and evidence hooks once per spec file.
 *
 * Passing tests delete their sessions and Agents. Failed, timed-out and
 * interrupted tests keep both sandboxes and attach the client session, its
 * child runs, public history, database-backed native Store, the server's own
 * log and the browser's stream bodies as independent diagnostics.
 */
export function trackNativeMcpScene(evidenceName: string): NativeMcpScene {
  const scene: NativeMcpScene = {
    sessions: trackSessions(), agents: [], serverSession: '', clientSession: '', serverLog: '', serverUrl: '',
  };
  onPassOnly(async ({ request }) => {
    const api = new AstraApi(request);
    for (const id of scene.agents) await api.deleteAgent(id);
  });
  test.afterEach(async ({ page, request }, info) => {
    if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
    const api = new AstraApi(request);
    const { sessions: _sessions, ...identity } = scene;
    const evidence = await Promise.all([
      observe(() => api.getSession(scene.clientSession)),
      observe(() => api.listChildRuns(scene.clientSession)),
      observe(() => api.getMessages(scene.clientSession)),
      observe(() => documentsByField('transcript_entries', '$.platform_session_id', scene.clientSession)),
      observe(() => serverLog(api, scene)),
      observe(() => aiStreamBodies(page)),
    ]);
    await info.attach(evidenceName, {
      body: JSON.stringify({ scene: identity, session: evidence[0], children: evidence[1], history: evidence[2],
        native_store: evidence[3], server_log: evidence[4], browser_stream: evidence[5] }, null, 2),
      contentType: 'application/json',
    });
  });
  return scene;
}

/**
 * Start the fixture server in its own conversation sandbox and resolve the
 * exposed URL the client sandbox will dial. Returns the process identity and
 * the MCP SDK version actually installed in the image.
 */
export async function startNativeMcpServer(
  api: AstraApi,
  scene: NativeMcpScene,
  options: { runId: string; tool: NativeMcpTool; port?: number },
): Promise<NativeMcpServerStart> {
  const port = options.port ?? NATIVE_MCP_PORT;
  const serverAgent = await api.createColdTestAgent(`__e2e_mcp_server_${options.runId}`);
  scene.agents.push(serverAgent.agent_id);
  scene.serverSession = (await api.startConversation(serverAgent.agent_id)).session_id;
  scene.sessions.push(scene.serverSession);
  await api.waitForSessionReady(scene.serverSession);
  const file = `astrabox-mcp-${options.tool}-${options.runId}.py`;
  const script = `/workspace/${file}`;
  scene.serverLog = `/workspace/astrabox-mcp-${options.tool}-${options.runId}.jsonl`;
  const outputLog = `/tmp/astrabox-mcp-${options.tool}-${options.runId}.log`;
  await api.uploadFileText(scene.serverSession, '/workspace', file, readFileSync(
    join(__dirname, 'native_mcp_server.py'), 'utf8',
  ));
  const started = await pythonJson(api, scene.serverSession, [
    'import json, socket, subprocess, sys, time',
    'from pathlib import Path',
    'from importlib.metadata import version',
    'mcp_version = version("mcp")',
    `output = Path(${JSON.stringify(outputLog)})`,
    'with output.open("x") as log:',
    `    process = subprocess.Popen([sys.executable, ${JSON.stringify(script)}, "--port", "${port}", "--tool", ${JSON.stringify(options.tool)}, "--log", ${JSON.stringify(scene.serverLog)}], stdout=log, stderr=log, start_new_session=True)`,
    'deadline = time.monotonic() + 10',
    'while True:',
    '    if process.poll() is not None: raise RuntimeError(f"MCP {mcp_version}, Python {sys.executable}:\\n" + output.read_text())',
    '    try:',
    `        with socket.create_connection(("127.0.0.1", ${port}), timeout=0.2): break`,
    '    except OSError:',
    '        if time.monotonic() >= deadline: raise RuntimeError(output.read_text())',
    '        time.sleep(0.1)',
    'print("E2E_MCP_JSON:" + json.dumps({"pid": process.pid, "mcp_version": mcp_version}))',
  ].join('\n')) as NativeMcpServerStart;

  const endpoint = await api.data<{ url: string; port: number }>(
    'GET', `/exposed-ports/${scene.serverSession}/${port}/url`,
  );
  expect(endpoint.port).toBe(port);
  expect(endpoint.url).toMatch(/^https?:\/\//);
  expect(endpoint.url).not.toContain('/api/v1/exposed-ports/');
  const endpointUrl = new URL(endpoint.url);
  endpointUrl.pathname = `${endpointUrl.pathname.replace(/\/$/, '')}/mcp`;
  scene.serverUrl = endpointUrl.toString();
  return started;
}

/**
 * Create the client Agent on the deployed MCP-enabled Environment, then open
 * its conversation. Only the schema-required identity fields, the version and the
 * given configuration are sent; `engine_options` is sent only when a case
 * supplies vendor env, so a case without one adds no inert knob.
 */
export async function configureNativeMcpClientAgent(
  api: AstraApi,
  scene: NativeMcpScene,
  options: {
    runId: string; label: string; server: string;
    env?: Record<string, string>; headers?: Record<string, string>;
  },
): Promise<AgentRecord> {
  expect(scene.serverUrl, 'the server must be running before the client Agent is configured').not.toEqual('');
  const environmentName = String(process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '').trim();
  expect(environmentName, 'the MCP client requires the deployed extension Environment').not.toEqual('');
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const environment = environments.find((row) => row.name === environmentName);
  expect(environment, `MCP client Environment ${environmentName}`).toMatchObject({
    enabled: true, engine_kind: 'claude_code',
    networking: { type: 'limited', allow_mcp_servers: true },
  });
  const base = await api.defaultAgent();
  const clientAgent = await api.createAgent({
    name: `__e2e_mcp_${options.label}_${options.runId}`,
    model: await api.configuredAgentModel(base.name, environmentName),
    environment_name: environmentName,
    prewarm_enabled: false,
  });
  scene.agents.push(clientAgent.agent_id);
  const configured = await api.updateAgent(clientAgent.agent_id, {
    name: clientAgent.name,
    model: clientAgent.model,
    environment_name: clientAgent.environment_name,
    version: clientAgent.version,
    ...(options.env ? { engine_options: { sdk_options: { env: options.env } } } : {}),
    mcp_servers: { [options.server]: {
      type: 'http', url: scene.serverUrl,
      ...(options.headers ? { headers: options.headers } : {}),
    } },
  });
  scene.clientSession = (await api.startConversation(clientAgent.agent_id)).session_id;
  scene.sessions.push(scene.clientSession);
  await api.waitForSessionReady(scene.clientSession);
  return configured;
}

/**
 * Reach the real exposed endpoint from the actual client sandbox through the
 * official MCP client, before any model turn. Discovery lists tools with their
 * `_meta`; it never invokes one.
 */
export async function discoverNativeMcpTools(
  api: AstraApi,
  scene: NativeMcpScene,
): Promise<DiscoveredNativeMcpTool[]> {
  const discovery = await pythonJson(api, scene.clientSession, [
    'import json',
    'import asyncio',
    'from mcp import Client',
    'async def main():',
    `    async with Client(${JSON.stringify(scene.serverUrl)}) as client:`,
    '        result = await client.list_tools()',
    '        print("E2E_MCP_JSON:" + json.dumps([{"name": tool.name, "meta": tool.meta} for tool in result.tools]))',
    'asyncio.run(main())',
  ].join('\n'));
  expect(Array.isArray(discovery), 'tool discovery returns a list').toBe(true);
  return discovery as DiscoveredNativeMcpTool[];
}
