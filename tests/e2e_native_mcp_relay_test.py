"""Real sockets prove the MCP fixture owns both sides of every forwarded call."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

DRIVER = r"""
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createServer, request } from 'node:http';
import { startNativeMcpRelay } from './tests/e2e-ui/fixtures/nativeMcpRelay.ts';

const scenario = process.argv[1];
const errors = [];
let arrive;
const arrived = new Promise(resolve => { arrive = resolve; });
let finish;
const finished = new Promise(resolve => { finish = resolve; });
let requestBody = '';
const origin = createServer((req, res) => {
  req.on('data', chunk => { requestBody += chunk; });
  req.on('end', () => {
    assert.equal(req.url, '/mcp');
    assert.equal(req.headers['mcp-session-id'], 'session-under-test');
    arrive(res);
    if (scenario === 'complete') {
      res.writeHead(201, { 'mcp-session-id': 'reply-session' });
      res.end(requestBody);
    } else if (scenario !== 'cancel-before-headers') {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: started\n\n');
    }
  });
  res.on('close', finish);
});
origin.listen(0, '127.0.0.1');
await once(origin, 'listening');
const port = origin.address().port;
const relay = await startNativeMcpRelay('127.0.0.1', new URL(`http://127.0.0.1:${port}`), port, errors, 0);
let relayClosed = false;
try {
  if (scenario === 'declared-port') {
    const listenPort = Number(relay.url.port);
    await assert.rejects(
      startNativeMcpRelay('127.0.0.1', new URL(`http://127.0.0.1:${port}`), port, errors, listenPort),
      { code: 'EADDRINUSE' },
    );
    await relay.close();
    relayClosed = true;
    const rebound = await startNativeMcpRelay(
      '127.0.0.1', new URL(`http://127.0.0.1:${port}`), port, errors, listenPort,
    );
    try { assert.equal(Number(rebound.url.port), listenPort); }
    finally { await rebound.close(); }
  } else if (scenario === 'wrong-route') {
    const denied = await fetch(new URL('/mcp', relay.url));
    assert.equal(denied.status, 404);
    assert.equal(requestBody, '');
  } else {
    let receive;
    const received = new Promise(resolve => { receive = resolve; });
    const client = request(`${relay.url}/mcp`, {
      method: 'POST', agent: false, headers: { 'mcp-session-id': 'session-under-test' },
    }, receive);
    client.on('error', () => {});
    client.end('{"method":"tools/call"}');
    const response = await arrived;
    let downstream;
    let downstreamClosed;
    if (scenario !== 'cancel-before-headers') {
      downstream = await received;
      downstream.on('error', () => {});
      downstreamClosed = new Promise(resolve => downstream.once('close', resolve));
    }
    if (scenario === 'complete') {
      let text = '';
      for await (const chunk of downstream) text += chunk;
      assert.equal(downstream.statusCode, 201);
      assert.equal(downstream.headers['mcp-session-id'], 'reply-session');
      assert.equal(text, '{"method":"tools/call"}');
    } else if (scenario === 'teardown') {
      await relay.close();
      relayClosed = true;
      await downstreamClosed;
    } else if (scenario === 'upstream-abort') {
      response.destroy();
      downstream.resume();
      await downstreamClosed;
      assert.equal(downstream.complete, false);
      assert.equal(errors.length, 1, JSON.stringify(errors));
    } else {
      client.destroy();
    }
    await finished;
    assert.equal(requestBody, '{"method":"tools/call"}');
  }
  if (scenario !== 'upstream-abort') assert.deepEqual(errors, []);
} finally {
  if (!relayClosed) await relay.close();
  const closed = new Promise(resolve => origin.close(resolve));
  origin.closeAllConnections();
  await closed;
}
console.log('PASS ' + scenario);
"""


@pytest.mark.parametrize(
    "scenario",
    ["complete", "wrong-route", "cancel", "cancel-before-headers", "teardown", "upstream-abort", "declared-port"],
)
def test_relay_stream_lifetime(scenario: str, node_toolchain_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", DRIVER, scenario],
        cwd=REPO,
        env=node_toolchain_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {scenario}" in result.stdout
