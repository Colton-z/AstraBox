import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import test from 'node:test'

import { IMAP } from '@satorijs/adapter-mail'
import { Universal } from '@satorijs/core'
import { WebSocketServer } from 'ws'

import { loadProviderCatalog } from '../src/providers.mjs'
import { BindingRuntime } from '../src/runtime.mjs'

function runtime(credentials = {}) {
  const value = new BindingRuntime({
    id: 'binding-1',
    provider: { name: 'test' },
    config: {},
    credentials,
    callbackBaseUrl: '',
    cursor: 0,
    onIdle: async () => {},
  })
  value.ctx = { server: { port: 12345 }, bots: [] }
  return value
}

test('a platform callback is not answered before its event is durably acknowledged', async (t) => {
  const binding = runtime()
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = async () => {
    binding.onSession({
      toJSON: () => ({ type: 'message-created', message: { id: 'message-1' } }),
    })
    return new Response('accepted', { status: 202, headers: { 'x-provider': 'ok' } })
  }

  let settled = false
  const pending = binding.callback({
    method: 'POST',
    path: '/callback',
    query: '',
    headers: {},
    body_base64: '',
  }).then((value) => {
    settled = true
    return value
  })
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(settled, false)
  assert.equal(binding.buffer.length, 1)
  binding.ack(binding.buffer[0].sn)
  assert.deepEqual(await pending, {
    status: 202,
    headers: { 'content-type': 'text/plain;charset=UTF-8', 'x-provider': 'ok' },
    body_base64: Buffer.from('accepted').toString('base64'),
  })
})

test('adapter delivery errors cannot echo configured credentials', async () => {
  const binding = runtime({ token: 'platform-token-value' })
  binding.ctx.bots.push({
    platform: 'test',
    selfId: 'bot-1',
    createMessage: async () => {
      throw new Error('request failed with platform-token-value')
    },
  })

  await assert.rejects(
    binding.deliver({
      platform: 'test',
      self_id: 'bot-1',
      channel_id: 'room-1',
      text: 'hello',
    }),
    (error) => error.message === 'request failed with [redacted]',
  )
})

test('a Matrix binding registers its application service and serves authenticated callbacks', async (t) => {
  const homeserverRequests = []
  const homeserver = createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const rawBody = Buffer.concat(chunks).toString('utf8')
    const url = new URL(request.url, 'http://matrix.example.test')
    homeserverRequests.push({
      method: request.method,
      path: url.pathname,
      authorization: request.headers.authorization,
      body: rawBody ? JSON.parse(rawBody) : undefined,
    })

    let status = 200
    let payload
    if (url.pathname === '/_matrix/client/v3/register') {
      payload = {
        access_token: 'matrix-access-token',
        user_id: '@astra-e2e:matrix.example.test',
      }
    } else if (decodeURIComponent(url.pathname) === '/_matrix/client/v3/profile/@astra-e2e:matrix.example.test') {
      payload = { displayname: 'Astra E2E' }
    } else if (url.pathname === '/_matrix/client/v3/sync') {
      payload = {
        next_batch: 'batch-1',
        rooms: { join: { '!room:matrix.example.test': {} } },
      }
    } else {
      status = 404
      payload = { errcode: 'M_NOT_FOUND' }
    }
    const body = JSON.stringify(payload)
    response.writeHead(status, {
      'content-length': Buffer.byteLength(body),
      'content-type': 'application/json',
    })
    response.end(body)
  })
  await new Promise((resolve, reject) => {
    homeserver.once('error', reject)
    homeserver.listen(0, '127.0.0.1', resolve)
  })
  t.after(() => new Promise((resolve, reject) => {
    homeserver.close((error) => error ? reject(error) : resolve())
  }))

  const catalog = await loadProviderCatalog()
  const provider = catalog.get('matrix')
  const binding = new BindingRuntime({
    id: 'matrix-binding',
    provider,
    config: {
      id: 'astra-e2e',
      host: 'matrix.example.test',
      endpoint: `http://127.0.0.1:${homeserver.address().port}`,
    },
    credentials: { hsToken: 'homeserver-token', asToken: 'application-service-token' },
    callbackBaseUrl: 'https://agents.example.test/callback',
    cursor: 0,
    onIdle: async () => {},
  })
  t.after(() => binding.stop())
  await binding.start()

  assert.deepEqual(homeserverRequests, [{
    method: 'POST',
    path: '/_matrix/client/v3/register',
    authorization: 'Bearer application-service-token',
    body: { type: 'm.login.application_service', username: 'astra-e2e' },
  }, {
    method: 'GET',
    path: '/_matrix/client/v3/profile/@astra-e2e:matrix.example.test',
    authorization: 'Bearer matrix-access-token',
    body: undefined,
  }, {
    method: 'GET',
    path: '/_matrix/client/v3/sync',
    authorization: 'Bearer matrix-access-token',
    body: undefined,
  }])
  assert.equal(binding.ctx.bots[0].status, Universal.Status.ONLINE)

  const accepted = await binding.callback({
    method: 'GET',
    path: '/matrix/_matrix/app/v1/users/@astra-e2e:matrix.example.test',
    query: 'access_token=homeserver-token',
    headers: {},
    body_base64: '',
  })
  assert.equal(accepted.status, 200, JSON.stringify(accepted))
  assert.equal(Buffer.from(accepted.body_base64, 'base64').toString('utf8'), '{}')

  const denied = await binding.callback({
    method: 'GET',
    path: '/matrix/_matrix/app/v1/users/@astra-e2e:matrix.example.test',
    query: 'access_token=wrong-token',
    headers: {},
    body_base64: '',
  })
  assert.equal(denied.status, 403)

  let resolveEvent
  const inboundEvent = new Promise((resolve) => { resolveEvent = resolve })
  const frames = []
  binding.attach({
    readyState: 1,
    send(value) {
      const frame = JSON.parse(value)
      frames.push(frame)
      if (frame.op === 0 && frame.body.type === 'message-created') resolveEvent(frame)
    },
    close() {},
  }, 0)
  const transactionBody = JSON.stringify({
    events: [{
      type: 'm.room.message',
      sender: '@user:matrix.example.test',
      room_id: '!room:matrix.example.test',
      event_id: '$event-1',
      origin_server_ts: Date.now(),
      content: { msgtype: 'm.text', body: 'hello from Matrix' },
    }],
  })
  let callbackSettled = false
  const transaction = binding.callback({
    method: 'PUT',
    path: '/matrix/_matrix/app/v1/transactions/txn-1',
    query: 'access_token=homeserver-token',
    headers: { 'content-type': 'application/json' },
    body_base64: Buffer.from(transactionBody).toString('base64'),
  }).then((response) => {
    callbackSettled = true
    return response
  })
  const event = await inboundEvent
  assert.equal(callbackSettled, false)
  binding.ack(event.body.sn)
  assert.equal((await transaction).status, 200)
  assert.equal(event.body.message?.content, 'hello from Matrix', JSON.stringify(event))

  const duplicate = await binding.callback({
    method: 'PUT',
    path: '/matrix/_matrix/app/v1/transactions/txn-1',
    query: 'access_token=homeserver-token',
    headers: { 'content-type': 'application/json' },
    body_base64: Buffer.from(transactionBody).toString('base64'),
  })
  assert.equal(duplicate.status, 200)
  assert.equal(
    frames.filter((frame) => frame.op === 0 && frame.body.type === 'message-created').length,
    1,
  )

  const callbackOrigin = `http://127.0.0.1:${binding.ctx.server.port}`
  await binding.stop()
  await assert.rejects(fetch(`${callbackOrigin}/matrix/_matrix/app/v1/users/ignored`))
})

test('an official adapter connection is stopped with its binding runtime', async (t) => {
  const originalConnect = IMAP.prototype.connect
  const originalStop = IMAP.prototype.stop
  const lifecycle = []
  IMAP.prototype.connect = async function (bot) {
    lifecycle.push('connect')
    bot.online()
  }
  IMAP.prototype.stop = async function () {
    lifecycle.push('stop')
  }
  t.after(() => {
    IMAP.prototype.connect = originalConnect
    IMAP.prototype.stop = originalStop
  })

  const catalog = await loadProviderCatalog()
  const binding = new BindingRuntime({
    id: 'mail-binding',
    provider: catalog.get('mail'),
    config: {
      username: 'bot@example.test',
      'imap.host': 'imap.example.test',
      'smtp.host': 'smtp.example.test',
    },
    credentials: { password: 'mail-password' },
    callbackBaseUrl: '',
    cursor: 0,
    onIdle: async () => {},
  })
  t.after(() => binding.stop())

  await binding.start()
  assert.equal(binding.ctx.bots[0].status, Universal.Status.ONLINE)
  const callbackOrigin = `http://127.0.0.1:${binding.ctx.server.port}`
  await binding.stop()
  assert.deepEqual(lifecycle, ['connect', 'stop'])
  await assert.rejects(fetch(`${callbackOrigin}/`))
})

test('the official Satori adapter bridges an existing protocol server and disconnects cleanly', async (t) => {
  const upstream = new WebSocketServer({ host: '127.0.0.1', port: 0, path: '/v1/events' })
  await new Promise((resolve, reject) => {
    upstream.once('listening', resolve)
    upstream.once('error', reject)
  })
  t.after(() => new Promise((resolve, reject) => {
    upstream.close((error) => error ? reject(error) : resolve())
  }))

  let identifyResolve
  const identified = new Promise((resolve) => { identifyResolve = resolve })
  let closeResolve
  const disconnected = new Promise((resolve) => { closeResolve = resolve })
  upstream.once('connection', (socket) => {
    socket.once('message', (data) => {
      identifyResolve(JSON.parse(data.toString()))
      socket.send(JSON.stringify({
        op: 4,
        body: {
          logins: [{
            sn: 1,
            platform: 'remote-test',
            status: Universal.Status.ONLINE,
            user: { id: 'remote-bot' },
          }],
          proxy_urls: [],
        },
      }))
    })
    socket.once('close', closeResolve)
  })

  const catalog = await loadProviderCatalog()
  const binding = new BindingRuntime({
    id: 'satori-binding',
    provider: catalog.get('satori'),
    config: { endpoint: `http://127.0.0.1:${upstream.address().port}` },
    credentials: { token: 'satori-access-token' },
    callbackBaseUrl: '',
    cursor: 0,
    onIdle: async () => {},
  })
  t.after(() => binding.stop())

  await binding.start()
  assert.deepEqual(await identified, { op: 3, body: { token: 'satori-access-token' } })
  const readyDeadline = Date.now() + 2_000
  while (!binding.ctx.bots.length && Date.now() < readyDeadline) {
    await new Promise((resolve) => setImmediate(resolve))
  }
  assert.equal(binding.ctx.bots[0]?.platform, 'remote-test')
  await binding.stop()
  await disconnected
})
