/** Prove Pi's public loader accepts native direct MCP registrations from the image package. */
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { discoverAndLoadExtensions } from '@earendil-works/pi-coding-agent';

const root = await mkdtemp(join(tmpdir(), 'astrabox-pi-mcp-'));
try {
  const packagePath = new URL('./node_modules/astrabox-platform-mcp/', import.meta.url).pathname;
  const url = 'http://127.0.0.1:1/build-only/mcp';
  process.env.ASTRABOX_PLATFORM_MCP_SERVERS = JSON.stringify([
    { name: 'astrabox_build_probe', url },
  ]);
  const result = await discoverAndLoadExtensions([packagePath], root, root);
  assert.deepEqual(result.errors, []);
  assert.equal(result.extensions.length, 1);
  assert.deepEqual(result.runtime.mcpServers.list().map(({ name, config }) => ({ name, config })), [
    { name: 'astrabox_build_probe', config: { url, exposure: 'direct' } },
  ]);
  const extension = result.extensions[0];
  assert.equal(extension.tools.size, 0);
  assert.equal(extension.commands.size, 0);
  assert.equal(extension.handlers.size, 0);
} finally {
  await rm(root, { recursive: true, force: true });
}
