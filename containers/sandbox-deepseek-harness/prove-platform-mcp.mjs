/** Build-only import and native configuration proof; no model or tool call. */
import assert from 'node:assert/strict';
import { Config, inject } from '@deepseek-ai/dsh-mcp-client';
import * as platform from './platform-mcp.mjs';

assert.equal(typeof platform.apply, 'function');
assert.deepEqual(platform.inject, inject);
const config = Config({
  serverName: 'astrabox_build_probe',
  transport: 'streamable-http',
  url: 'http://127.0.0.1:1/mcp',
  failOnStartupError: true,
  reconnect: { enabled: false },
});
assert.equal(config.transport, 'streamable-http');
assert.equal(config.failOnStartupError, true);
assert.equal(config.reconnect.enabled, false);
