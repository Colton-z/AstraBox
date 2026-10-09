/**
 * E2E: an Agent's program calls an MCP server its author declares.
 *
 * MCP servers are the portable part of an Agent's extensions: each engine
 * adapter renders the author's server into its own configuration (Codex the
 * thread's `config.mcp_servers`, Pi its user-level `mcp.json`, DeepSeek
 * Harness a `dsh-mcp-client` row in its home patch layer). The server
 * here is a stdio one the box's own Python runs with the MCP SDK the image
 * carries, so it needs no network.
 * Its one tool returns a marker the server receives only through the `env`
 * the author declared, so the marker can reach the reply only through a real
 * call to that server, whichever way the engine exposes its tools (Pi's
 * default reaches MCP tools from a `codemode` script, not as declared tools).
 * An engine may gate the call the way it gates any unannotated MCP tool
 * (Codex asks through its native elicitation); the spec posts the turn,
 * answers each gate while the turn waits for it, as the Agent's user would,
 * and reads the reply from the stored conversation once the turn settles.
 * The conversation claims the Agent's prewarmed box, where the server
 * definition was already in place.
 *
 * The HTTP cases run on each engine's conversation-tenancy Environment, where
 * the definition is written when the conversation's own box is provisioned.
 * Their server is the shared HTTP MCP fixture in a box of its own, reached at
 * its exposed address, which an administrator's copy of the Environment
 * admits; the server logs every call it serves, so the evidence of a real
 * call is the server's own record rather than the reply.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { approveGates } from '../fixtures/approveGates';
import { AstraApi, messageText } from '../fixtures/astraApi';
import { engineProfileFor } from '../fixtures/engineProfile';
import {
  configureNativeMcpClientAgent, serverLog, startNativeMcpServer, trackNativeMcpScene,
} from '../fixtures/nativeMcpServer';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

/** The image's own interpreter; the MCP Python SDK is installed for it. */
const PYTHON = '/usr/local/bin/python3.12';
const SERVER = 'e2e_marker';
const HTTP_SERVER = 'e2e_http';
const SERVER_SOURCE = [
  'import os',
  'from mcp.server.fastmcp import FastMCP',
  'app = FastMCP("e2e-marker")',
  '@app.tool()',
  'def read_marker() -> str:',
  '    """Return this server\'s marker."""',
  '    return os.environ["E2E_MARKER"]',
  'app.run()',
].join('\n');

const sessions = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds) await api.deleteAgent(agentId);
});

for (const engineKind of ['codex', 'pi', 'deepseek_harness'] as const) {
  test(`${engineKind}: an Agent calls the stdio MCP server its author declares`, async ({ request }) => {
    const api = new AstraApi(request);
    const profile = engineProfileFor(engineKind);
    const marker = `E2E_MCP_MARKER_${randomUUID().replace(/-/g, '').slice(0, 16)}`;
    const agent = await api.createAgent({
      name: `__e2e_author_mcp_${engineKind}_${randomUUID().slice(0, 8)}`,
      model: profile.model,
      environment_name: profile.environment_name,
      prepared_capacity: 1,
      mcp_servers: {
        [SERVER]: { type: 'stdio', command: PYTHON, args: ['-c', SERVER_SOURCE], env: { E2E_MARKER: marker } },
      },
    });
    agentIds.push(agent.agent_id);

    const platform = new PlatformApi(request);
    let prepared: Record<string, unknown> = {};
    await expect.poll(async () => {
      prepared = await platform.preparedRuntime(agent.agent_id);
      return prepared.ready === true && Number(prepared.prepared_count || 0) > 0 && Boolean(prepared.sandbox_id);
    }, { timeout: 120_000, intervals: [2_000], message: 'the Agent must prepare a runtime' }).toBe(true);
    const sessionId = String((await api.startConversation(agent.agent_id)).session_id || '');
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId, 120_000);
    expect(ready.sandbox_id, 'the conversation claims the prepared box').toBe(prepared.sandbox_id);

    // Posted rather than streamed: a gate raised while no stream holds the
    // turn waits parked for its answer, as it does for a user.
    await api.postTurnInput(sessionId, [
      `Call the read_marker tool of the ${SERVER} MCP server once,`,
      'then reply with exactly the text it returned and nothing else.',
    ].join(' '));
    await approveGates(api, sessionId);
    const reply = (await api.getMessages(sessionId)).messages
      .filter((message) => message.role === 'assistant').map(messageText).join('\n');
    expect(reply, 'only the server knows the marker').toContain(marker);
  });
}

test.describe('HTTP on conversation tenancy', () => {
  // One listener at a time: a direct-endpoint relay holds one fixed port per tool.
  test.describe.configure({ mode: 'serial' });
  const scene = trackNativeMcpScene('author-http-mcp-scene');

  for (const engineKind of ['codex', 'pi', 'deepseek_harness'] as const) {
    test(`${engineKind}: an Agent on conversation tenancy calls the HTTP MCP server its author declares`, async ({ request }) => {
      const api = new AstraApi(request);
      const profile = engineProfileFor(engineKind);
      // The fixture configures one Environment per tenancy, the other named
      // with that tenancy as suffix (configure-agent-engine-matrix.py).
      const environment = `${profile.environment_name}-conversation`;
      const stored = (await new PlatformApi(request).listEnvironments())
        .find((item) => String(item.name || '') === environment);
      expect(stored?.sandbox_tenancy, `${environment} is the conversation-tenancy Environment`).toBe('conversation');

      const runId = `${Date.now()}-${test.info().workerIndex}`;
      const input = {
        head_marker: `E2E_HTTP_MCP_HEAD_${runId}`, tail_marker: `E2E_HTTP_MCP_TAIL_${runId}`, output_size: 256,
      };
      const done = `DONE_E2E_HTTP_MCP_${runId}`;
      await startNativeMcpServer(api, scene, { runId, tool: 'large_output' });
      const agent = await configureNativeMcpClientAgent(api, scene, {
        runId, label: `author_${engineKind}`, server: HTTP_SERVER,
        environment: { name: environment, engineKind, model: profile.model },
      });
      expect((await api.getAgent(agent.agent_id)).mcp_servers)
        .toEqual({ [HTTP_SERVER]: { type: 'http', url: scene.serverUrl } });

      await api.postTurnInput(scene.clientSession, [
        `Call the large_output tool of the ${HTTP_SERVER} MCP server exactly once,`,
        `with the arguments ${JSON.stringify(input)}.`,
        `Do not call any other tool. After it returns, reply with exactly ${done} and nothing else.`,
      ].join(' '));
      await approveGates(api, scene.clientSession);
      const settled = await api.getSession(scene.clientSession);
      expect(settled.last_turn_status, String(settled.last_error || '')).toBe('COMPLETED');

      const log = await serverLog(api, scene);
      expect(log, 'the server served exactly one call').toHaveLength(1);
      expect(log[0]).toMatchObject({ tool: 'large_output', phase: 'returned', request_method: 'tools/call', ...input });
      const reply = (await api.getMessages(scene.clientSession)).messages
        .filter((message) => message.role === 'assistant').map(messageText).join('\n');
      expect(reply).toContain(done);
    });
  }
});
