import { createHash } from 'node:crypto'

import { HTTP } from '@cordisjs/plugin-http'
import Server from '@cordisjs/plugin-server'
import { Context, h, Universal } from '@satorijs/core'
import { snakeCase } from 'cosmokit'

import { redactError, validateProvider } from './providers.mjs'

const EVENT = 0
const READY = 4
const STOP = 6
const ACK = 7
const BUFFER_LIMIT = 1024
const RECONNECT_GRACE_MS = 30_000
const CALLBACK_ACK_TIMEOUT_MS = 10_000
const ACK_HOOK = Symbol.for('astrabox.channel.ack')

let nextPrivatePort = 20_000

function stable(value) {
  if (Array.isArray(value)) return value.map(stable)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]))
  }
  return value
}

function fingerprint(value) {
  return createHash('sha256').update(JSON.stringify(stable(value))).digest('hex')
}

function send(socket, payload) {
  if (socket.readyState === 1) socket.send(JSON.stringify(payload))
}

class GatewayUnavailableError extends Error {
  constructor(message) {
    super(message)
    this.statusCode = 503
  }
}

function redactedError(error, credentials) {
  const wrapped = new Error(redactError(error, credentials))
  if (Number.isInteger(error?.statusCode)) wrapped.statusCode = error.statusCode
  return wrapped
}

async function closeServer(server) {
  if (!server?.listening) return
  await new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve())
  })
}

async function closeWebSocketServer(server) {
  if (!server) return
  for (const client of server.clients) client.terminate()
  await new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve())
  })
}

export class BindingRuntime {
  constructor({ id, provider, config, credentials, callbackBaseUrl, cursor, onIdle }) {
    this.id = id
    this.provider = provider
    this.visibleConfig = config
    this.credentials = credentials
    this.callbackBaseUrl = callbackBaseUrl
    this.nextCursor = Math.max(Number.isSafeInteger(cursor) ? cursor + 1 : 1, Date.now() * 1000)
    this.onIdle = onIdle
    this.clients = new Set()
    this.buffer = []
    this.ackHooks = new Map()
    this.stopTimer = null
    this.ctx = null
    this.adapterScope = null
    this.callbackTail = Promise.resolve()
    this.activeCallback = null
    this.signature = fingerprint({ provider: provider.name, config, credentials, callbackBaseUrl })
  }

  async start() {
    let validated
    try {
      validated = await validateProvider(
        this.provider,
        this.visibleConfig,
        this.credentials,
      )
    } catch (error) {
      throw new Error(redactError(error, this.credentials))
    }
    const ctx = new Context()
    this.ctx = ctx
    ctx.on('bot-added', (bot) => { bot.user ??= {} })
    ctx.plugin(HTTP)
    const port = nextPrivatePort
    nextPrivatePort = nextPrivatePort >= 59_999 ? 20_000 : nextPrivatePort + 1
    ctx.plugin(Server, {
      host: '127.0.0.1',
      port,
      maxPort: 60_000,
      selfUrl: this.callbackBaseUrl || undefined,
    })
    ctx.on('internal/session', (session) => this.onSession(session))
    const adapterScope = ctx.plugin(validated.Adapter, validated.adapterConfig)
    this.adapterScope = adapterScope
    try {
      await ctx.start()
      if (adapterScope.error) throw adapterScope.error
    } catch (error) {
      try {
        await this.stop()
      } catch (cleanupError) {
        const startMessage = redactedError(error, this.credentials).message
        const cleanupMessage = redactedError(cleanupError, this.credentials).message
        throw new Error(`${startMessage}; channel cleanup failed: ${cleanupMessage}`)
      }
      throw redactedError(error, this.credentials)
    }
  }

  onSession(session) {
    try {
      const body = Universal.transformKey(session.toJSON(), snakeCase)
      if (body.type === 'message-deleted') {
        const original = session.event
        // Session.toJSON() replaces the upstream sn with the SDK Session's
        // local counter. Neither that counter nor the gateway ACK cursor
        // identifies a redelivered external deletion.
        body.source_event_key = Number.isSafeInteger(original.sn)
          ? `sn:${original.sn}`
          : `message-deleted:${fingerprint({
            timestamp: original.timestamp,
            channel: original.channel?.id,
            message: original.message?.id,
            user: original.user?.id,
          })}`
      }
      body.sn = this.nextCursor++
      const callback = this.activeCallback
      if (callback) {
        const prior = session[ACK_HOOK]
        const durable = new Promise((resolve, reject) => {
          session[ACK_HOOK] = async () => {
            try {
              if (typeof prior === 'function') await prior()
              resolve()
            } catch (error) {
              const failure = new GatewayUnavailableError('adapter acknowledgement failed')
              reject(failure)
              throw error
            }
          }
        })
        durable.catch(() => {})
        callback.pending.push(durable)
      }
      if (typeof session[ACK_HOOK] === 'function') {
        this.ackHooks.set(body.sn, session[ACK_HOOK])
      }
      this.buffer.push(body)
      if (this.buffer.length > BUFFER_LIMIT) {
        for (const client of this.clients) client.close(1013, 'channel event buffer exhausted')
        this.clients.clear()
        callback?.fail(new GatewayUnavailableError('channel event buffer exhausted'))
        void Promise.resolve(this.onIdle(this.id, this)).catch(() => {
          console.error(`[channel-gateway] binding ${this.id} could not stop after buffer exhaustion`)
        })
        return
      }
      for (const client of this.clients) send(client, { op: EVENT, body })
    } catch (error) {
      console.error(
        `[channel-gateway] binding ${this.id} could not serialize an adapter event: ${redactError(error, this.credentials)}`,
      )
    }
  }

  attach(socket, cursor) {
    if (this.stopTimer) clearTimeout(this.stopTimer)
    this.stopTimer = null
    this.clients.add(socket)
    this.ack(cursor)
    send(socket, {
      op: READY,
      body: { provider: this.provider.name },
    })
    for (const event of this.buffer) {
      if (event.sn > cursor) send(socket, { op: EVENT, body: event })
    }
  }

  detach(socket) {
    this.clients.delete(socket)
    if (this.clients.size) return
    this.stopTimer = setTimeout(() => void this.onIdle(this.id, this), RECONNECT_GRACE_MS)
    this.stopTimer.unref()
  }

  ack(cursor) {
    if (!Number.isSafeInteger(cursor) || cursor < 0) return
    for (const [sequence, hook] of this.ackHooks) {
      if (sequence > cursor) continue
      this.ackHooks.delete(sequence)
      Promise.resolve(hook()).catch(() => {
        console.error(`[channel-gateway] binding ${this.id} adapter acknowledgement failed`)
      })
    }
    this.buffer = this.buffer.filter((event) => event.sn > cursor)
    if (this.nextCursor <= cursor) this.nextCursor = cursor + 1
  }

  async deliver({ platform, self_id: selfId, channel_id: channelId, message_id: messageId, text }) {
    try {
      const bot = this.ctx?.bots.find(
        (candidate) => candidate.platform === platform && candidate.selfId === selfId,
      )
      if (!bot) throw new Error(`connected login ${platform}/${selfId} is unavailable`)
      if (!channelId || typeof text !== 'string') throw new Error('delivery routing is incomplete')
      const content = messageId ? [h('quote', { id: messageId }), text] : text
      const messages = await bot.createMessage(channelId, content)
      const ids = messages.map((message) => String(message?.id || '')).filter(Boolean)
      if (!ids.length) throw new Error('adapter returned no message receipt')
      return ids
    } catch (error) {
      throw redactedError(error, this.credentials)
    }
  }

  callback(payload) {
    const current = this.callbackTail.catch(() => {}).then(() => this.forwardCallback(payload))
    this.callbackTail = current
    return current
  }

  async forwardCallback({ method, path, query, headers, body_base64: bodyBase64 }) {
    try {
      if (!this.ctx?.server?.port) throw new Error('provider callback server is unavailable')
      let fail
      const failure = new Promise((_resolve, reject) => { fail = reject })
      failure.catch(() => {})
      const callback = { pending: [], failure, fail }
      this.activeCallback = callback
      try {
        const url = new URL(`http://127.0.0.1:${this.ctx.server.port}${path}`)
        if (query) url.search = query
        const forwardedHeaders = new Headers()
        for (const [key, value] of Object.entries(headers || {})) {
          if (!['host', 'content-length', 'connection', 'x-astrabox-gateway-token'].includes(key.toLowerCase())) {
            forwardedHeaders.set(key, String(value))
          }
        }
        const upperMethod = String(method || 'POST').toUpperCase()
        const body = ['GET', 'HEAD'].includes(upperMethod)
          ? undefined
          : Buffer.from(String(bodyBase64 || ''), 'base64')
        const response = await fetch(url, { method: upperMethod, headers: forwardedHeaders, body })
        if (callback.pending.length) {
          let timer
          const timeout = new Promise((_resolve, reject) => {
            timer = setTimeout(
              () => reject(new GatewayUnavailableError('durable callback acknowledgement timed out')),
              CALLBACK_ACK_TIMEOUT_MS,
            )
            timer.unref()
          })
          try {
            await Promise.race([Promise.all(callback.pending), callback.failure, timeout])
          } finally {
            clearTimeout(timer)
          }
        }
        const responseHeaders = {}
        for (const [key, value] of response.headers) responseHeaders[key] = value
        return {
          status: response.status,
          headers: responseHeaders,
          body_base64: Buffer.from(await response.arrayBuffer()).toString('base64'),
        }
      } finally {
        if (this.activeCallback === callback) this.activeCallback = null
      }
    } catch (error) {
      throw redactedError(error, this.credentials)
    }
  }

  async stop() {
    if (this.stopTimer) clearTimeout(this.stopTimer)
    this.stopTimer = null
    for (const client of this.clients) client.close(1001, 'binding stopped')
    this.clients.clear()
    const ctx = this.ctx
    this.ctx = null
    const adapterScope = this.adapterScope
    this.adapterScope = null
    if (!ctx) return
    const failures = []
    const settle = async (operation) => {
      try {
        await operation()
      } catch (error) {
        failures.push(redactError(error, this.credentials))
      }
    }
    await settle(() => this.callbackTail)
    const bots = [...ctx.bots]
    await settle(() => Promise.all(bots.map((bot) => bot.stop())))
    const adapters = new Set(bots.map((bot) => bot.adapter).filter(Boolean))
    await settle(() => Promise.all(
      [...adapters].filter((adapter) => typeof adapter.stop === 'function').map((adapter) => adapter.stop()),
    ))
    adapterScope?.dispose()
    ctx.lifecycle.isActive = false
    await settle(() => closeWebSocketServer(ctx.server?._ws))
    await settle(() => closeServer(ctx.server?._http))
    if (failures.length) {
      throw new Error(`channel binding cleanup failed: ${failures.map((error) => error.message).join('; ')}`)
    }
  }
}

export class RuntimeManager {
  constructor(catalog) {
    this.catalog = catalog
    this.runtimes = new Map()
    this.locks = new Map()
  }

  async serialize(id, operation) {
    const prior = this.locks.get(id) || Promise.resolve()
    const current = prior.catch(() => {}).then(operation)
    this.locks.set(id, current)
    try {
      return await current
    } finally {
      if (this.locks.get(id) === current) this.locks.delete(id)
    }
  }

  async attach(id, socket, identify) {
    return this.serialize(id, async () => {
      const provider = this.catalog.get(String(identify.provider || ''))
      if (!provider) throw new Error('identify names an unknown provider')
      const cursor = identify.cursor == null ? 0 : identify.cursor
      if (!Number.isSafeInteger(cursor) || cursor < 0) throw new Error('identify cursor must be a non-negative integer')
      const candidate = new BindingRuntime({
        id,
        provider,
        config: identify.config || {},
        credentials: identify.credentials || {},
        callbackBaseUrl: String(identify.callback_base_url || '').replace(/\/$/, ''),
        cursor,
        onIdle: (bindingId, runtime) => this.remove(bindingId, runtime),
      })
      let runtime = this.runtimes.get(id)
      if (!runtime || runtime.signature !== candidate.signature) {
        if (runtime) await runtime.stop()
        runtime = candidate
        await runtime.start()
        this.runtimes.set(id, runtime)
      }
      runtime.attach(socket, cursor)
      return runtime
    })
  }

  async remove(id, expected) {
    return this.serialize(id, async () => {
      if (this.runtimes.get(id) !== expected || expected.clients.size) return
      this.runtimes.delete(id)
      await expected.stop()
    })
  }

  async stop(id, socket) {
    return this.serialize(id, async () => {
      const runtime = this.runtimes.get(id)
      if (!runtime) return
      runtime.clients.delete(socket)
      if (runtime.clients.size) return
      this.runtimes.delete(id)
      await runtime.stop()
    })
  }

  detach(id, socket) {
    this.runtimes.get(id)?.detach(socket)
  }

  acknowledge(id, cursor) {
    this.runtimes.get(id)?.ack(cursor)
  }

  require(id) {
    const runtime = this.runtimes.get(id)
    if (!runtime) throw new Error('channel binding is not connected')
    return runtime
  }

  async close() {
    const runtimes = [...this.runtimes.values()]
    this.runtimes.clear()
    await Promise.all(runtimes.map((runtime) => runtime.stop()))
  }
}

export const GatewayOpcode = Object.freeze({ EVENT, READY, STOP, ACK })
