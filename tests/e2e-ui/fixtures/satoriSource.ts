/** An external Satori protocol server consumed by the real official adapter. */
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect } from '@playwright/test';

import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from './serviceContainer';

const SOURCE = String.raw`
import asyncio
import json
import socket
import sys
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

token, bot_id = sys.argv[1:]
app = FastAPI()
sockets = set()
sequence = 0
delivery_count = 0
login = {"sn": 1, "platform": "e2e", "user": {"id": bot_id, "name": "AstraBox"},
         "status": 1, "adapter": "satori"}

def report(kind, payload):
    print(json.dumps({"kind": kind, "payload": payload}), flush=True)

@app.websocket("/v1/events")
async def events(ws: WebSocket):
    await ws.accept()
    identify = await ws.receive_json()
    if identify.get("op") != 3 or identify.get("body", {}).get("token") != token:
        report("error", "official adapter sent invalid IDENTIFY")
        await ws.close(code=1008)
        return
    sockets.add(ws)
    await ws.send_json({"op": 4, "body": {"logins": [login], "proxy_urls": []}})
    report("connected", True)
    try:
        while True:
            signal = await ws.receive_json()
            if signal.get("op") != 1:
                raise RuntimeError("unexpected adapter signal")
            await ws.send_json({"op": 2, "body": {}})
    except WebSocketDisconnect:
        pass
    finally:
        sockets.discard(ws)

@app.post("/v1/message.create")
async def create_message(request: Request):
    global delivery_count
    if (request.headers.get("authorization") != f"Bearer {token}"
            or request.headers.get("satori-platform") != "e2e"
            or request.headers.get("satori-user-id") != bot_id):
        report("error", "official adapter sent invalid delivery credentials or login identity")
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "invalid delivery identity"}, status_code=403)
    payload = await request.json()
    if not isinstance(payload.get("content"), str) or not payload.get("channel_id"):
        raise RuntimeError("invalid Satori message.create payload")
    delivery_count += 1
    report("delivery", payload)
    return [{"id": f"reply-{delivery_count}", "content": payload["content"]}]

async def control(server):
    global sequence
    while True:
        raw = await asyncio.to_thread(sys.stdin.readline)
        if not raw:
            server.should_exit = True
            return
        command = json.loads(raw)
        if command.get("stop"):
            for ws in list(sockets):
                await ws.close()
            server.should_exit = True
            return
        if len(sockets) != 1:
            raise RuntimeError(f"expected one official adapter connection, got {len(sockets)}")
        sequence += 1
        event = {"sn": sequence, **command, "login": login}
        await next(iter(sockets)).send_json({"op": 0, "body": event})
        report("sent", {"sn": sequence, "type": event["type"]})

async def main():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("Satori fixture server did not start")
        await asyncio.sleep(0.01)
    report("ready", sock.getsockname()[1])
    controller = asyncio.create_task(control(server))
    done, _ = await asyncio.wait({task, controller}, return_when=asyncio.FIRST_COMPLETED)
    for completed in done:
        await completed
    server.should_exit = True
    await task
    controller.cancel()

asyncio.run(main())
`;

export async function satoriSource() {
  const token = randomUUID();
  const botId = `bot-${randomUUID()}`;
  const errors: string[] = [];
  const deliveries: Array<{ channel_id: string; content: string }> = [];
  const child = spawn('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE),
    'python', '-u', '-c', SOURCE, token, botId,
  ], { stdio: ['pipe', 'pipe', 'pipe'] });
  let port = 0;
  let connected = false;
  let sent = 0;
  let pending = '';
  let stderr = '';
  let exit: { code: number | null; signal: NodeJS.Signals | null } | null = null;
  child.stdout.on('data', (chunk: Buffer) => {
    pending += chunk.toString('utf8');
    for (;;) {
      const boundary = pending.indexOf('\n');
      if (boundary < 0) break;
      const line = pending.slice(0, boundary);
      pending = pending.slice(boundary + 1);
      try {
        const record = JSON.parse(line) as { kind: string; payload: unknown };
        if (record.kind === 'ready') port = Number(record.payload);
        if (record.kind === 'connected') connected = true;
        if (record.kind === 'sent') sent += 1;
        if (record.kind === 'error') errors.push(String(record.payload));
        if (record.kind === 'delivery') deliveries.push(record.payload as typeof deliveries[number]);
      } catch (error) { errors.push(`invalid fixture evidence: ${String(error)}; ${line}`); }
    }
  });
  child.stderr.on('data', (chunk: Buffer) => { stderr = `${stderr}${chunk.toString('utf8')}`.slice(-4000); });
  child.on('error', (error) => errors.push(error.message));
  const done = new Promise<void>((resolve) => child.once('exit', (code, signal) => {
    exit = { code, signal };
    resolve();
  }));
  function healthy() {
    if (errors.length || exit) throw new Error(`Satori source failed: ${JSON.stringify({ errors, exit, stderr })}`);
  }
  try {
    await expect.poll(() => { healthy(); return port; }, { timeout: 10_000 }).toBeGreaterThan(0);
  } catch (error) {
    child.stdin.end();
    throw error;
  }
  return {
    botId, token, endpoint: `http://127.0.0.1:${port}`, errors, deliveries,
    async waitConnected() {
      await expect.poll(() => { healthy(); return connected; }, { timeout: 30_000 }).toBe(true);
    },
    send(event: Record<string, unknown>) {
      healthy();
      if (!connected) throw new Error('Satori adapter has not connected');
      child.stdin.write(`${JSON.stringify(event)}\n`);
    },
    healthy,
    async waitSent(count: number) {
      await expect.poll(() => { healthy(); return sent; }).toBe(count);
    },
    async close() {
      if (!exit) child.stdin.end(`${JSON.stringify({ stop: true })}\n`);
      await Promise.race([done, new Promise<never>((_, reject) => {
        const timer = setTimeout(() => reject(new Error(`Satori fixture did not stop: ${stderr}`)), 10_000);
        void done.then(() => clearTimeout(timer));
      })]);
      if (errors.length || exit?.code !== 0) {
        throw new Error(`Satori source ended unsuccessfully: ${JSON.stringify({ errors, exit, stderr })}`);
      }
    },
  };
}
