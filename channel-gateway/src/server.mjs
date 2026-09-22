import { timingSafeEqual } from 'node:crypto'
import { createServer } from 'node:http'

import { WebSocketServer } from 'ws'

import {
  isRecord,
  loadProviderCatalog,
  publicDescriptor,
  redactError,
  validateProvider,
} from './providers.mjs'
import { GatewayOpcode, RuntimeManager } from './runtime.mjs'

const HOST = String(process.env.ASTRABOX_CHANNEL_GATEWAY_HOST || '127.0.0.1').trim()
const PORT = Number(process.env.ASTRABOX_CHANNEL_GATEWAY_PORT || 8765)
const TOKEN = String(process.env.ASTRABOX_CHANNEL_GATEWAY_TOKEN || '').trim()
const MAX_BODY_BYTES = 2 * 1024 * 1024

if (TOKEN.length < 32) {
  throw new Error('ASTRABOX_CHANNEL_GATEWAY_TOKEN must contain at least 32 characters')
}
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  throw new Error('ASTRABOX_CHANNEL_GATEWAY_PORT must be a TCP port')
}

function authorized(request) {
  const supplied = String(request.headers['x-astrabox-gateway-token'] || '')
  const left = Buffer.from(supplied)
  const right = Buffer.from(TOKEN)
  return left.length === right.length && timingSafeEqual(left, right)
}

function json(response, status, payload) {
  const body = JSON.stringify(payload)
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
  })
  response.end(body)
}

async function readJson(request) {
  const chunks = []
  let size = 0
  for await (const chunk of request) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) throw new Error('request body exceeds 2 MiB')
    chunks.push(chunk)
  }
  const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
  if (!isRecord(parsed)) throw new Error('request body must be a JSON object')
  return parsed
}

function bindingPath(pathname, suffix) {
  const match = pathname.match(new RegExp(`^/v1/bindings/([A-Za-z0-9_-]{1,128})/${suffix}$`))
  return match?.[1]
}

const catalog = await loadProviderCatalog()
const manager = new RuntimeManager(catalog)

const server = createServer(async (request, response) => {
  const url = new URL(request.url || '/', `http://${request.headers.host || 'localhost'}`)
  if (request.method === 'GET' && url.pathname === '/healthz') {
    return json(response, 200, { status: 'ok', providers: catalog.size })
  }
  if (!authorized(request)) return json(response, 401, { error: 'unauthorized' })

  try {
    if (request.method === 'GET' && url.pathname === '/v1/providers') {
      return json(response, 200, [...catalog.values()].map(publicDescriptor))
    }

    const validateMatch = url.pathname.match(/^\/v1\/providers\/([a-z0-9-]+)\/validate$/)
    if (request.method === 'POST' && validateMatch) {
      const provider = catalog.get(validateMatch[1])
      if (!provider) return json(response, 404, { error: 'provider not found' })
      const body = await readJson(request)
      let result
      try {
        result = await validateProvider(provider, body.config || {}, body.credentials || {})
      } catch (error) {
        // Adapter schemas are third-party code. Never trust their diagnostic
        // not to echo the candidate token they were asked to validate.
        throw new Error(redactError(error, body.credentials || {}))
      }
      return json(response, 200, { config: result.config, credentials: result.credentials })
    }

    const messageBinding = bindingPath(url.pathname, 'messages')
    if (request.method === 'POST' && messageBinding) {
      const body = await readJson(request)
      const ids = await manager.require(messageBinding).deliver(body)
      return json(response, 200, { message_ids: ids })
    }

    const callbackBinding = bindingPath(url.pathname, 'callback')
    if (request.method === 'POST' && callbackBinding) {
      const body = await readJson(request)
      const result = await manager.require(callbackBinding).callback(body)
      return json(response, 200, result)
    }

    return json(response, 404, { error: 'not found' })
  } catch (error) {
    const message = redactError(error)
    const status = Number.isInteger(error?.statusCode)
      ? error.statusCode
      : message === 'channel binding is not connected' ? 503 : 400
    return json(response, status, { error: message })
  }
})

const sockets = new WebSocketServer({ noServer: true })

server.on('upgrade', (request, socket, head) => {
  const url = new URL(request.url || '/', `http://${request.headers.host || 'localhost'}`)
  const bindingId = bindingPath(url.pathname, 'events')
  if (!bindingId || !authorized(request)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n')
    socket.destroy()
    return
  }
  sockets.handleUpgrade(request, socket, head, (websocket) => {
    sockets.emit('connection', websocket, request, bindingId)
  })
})

sockets.on('connection', (socket, _request, bindingId) => {
  let identified = false
  let runtimeAttached = false
  const identifyTimeout = setTimeout(() => socket.close(1008, 'identify timeout'), 15_000)
  identifyTimeout.unref()

  socket.on('message', async (data) => {
    try {
      const payload = JSON.parse(data.toString())
      if (!isRecord(payload)) throw new Error('websocket payload must be an object')
      if (!identified) {
        if (payload.op !== 3 || !isRecord(payload.body)) throw new Error('first payload must be IDENTIFY')
        identified = true
        clearTimeout(identifyTimeout)
        await manager.attach(bindingId, socket, payload.body)
        runtimeAttached = true
        return
      }
      if (payload.op === 1) {
        socket.send(JSON.stringify({ op: 2, body: {} }))
      } else if (payload.op === GatewayOpcode.ACK) {
        manager.acknowledge(bindingId, payload.body?.cursor)
      } else if (payload.op === GatewayOpcode.STOP) {
        await manager.stop(bindingId, socket)
      }
    } catch (error) {
      socket.close(1011, redactError(error).slice(0, 120))
    }
  })
  socket.on('close', () => {
    clearTimeout(identifyTimeout)
    if (runtimeAttached) manager.detach(bindingId, socket)
  })
})

async function shutdown() {
  sockets.close()
  await manager.close()
  server.close(() => process.exit(0))
  setTimeout(() => process.exit(1), 10_000).unref()
}

process.once('SIGTERM', shutdown)
process.once('SIGINT', shutdown)

server.listen(PORT, HOST, () => {
  console.log(`[channel-gateway] listening on http://${HOST}:${PORT}`)
})
