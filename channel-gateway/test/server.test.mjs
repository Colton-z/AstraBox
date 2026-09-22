import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import test from 'node:test'

const token = 'channel-gateway-test-token-000000000000'

async function unusedPort() {
  const server = createServer()
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  const port = address.port
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  return port
}

async function waitForHealth(baseUrl, child) {
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`gateway exited with ${child.exitCode}`)
    try {
      const response = await fetch(`${baseUrl}/healthz`)
      if (response.ok) return
    } catch {
      // Refused connections are expected while Node binds the socket.
    }
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  throw new Error('gateway did not become healthy')
}

test('the private gateway authenticates control routes and validates adapters', async (t) => {
  const port = await unusedPort()
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: new URL('..', import.meta.url),
    env: {
      ...process.env,
      ASTRABOX_CHANNEL_GATEWAY_HOST: '127.0.0.1',
      ASTRABOX_CHANNEL_GATEWAY_PORT: String(port),
      ASTRABOX_CHANNEL_GATEWAY_TOKEN: token,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let output = ''
  child.stdout.on('data', (value) => { output += value })
  child.stderr.on('data', (value) => { output += value })
  t.after(async () => {
    if (child.exitCode === null) {
      child.kill('SIGTERM')
      await new Promise((resolve) => child.once('exit', resolve))
    }
  })

  const baseUrl = `http://127.0.0.1:${port}`
  try {
    await waitForHealth(baseUrl, child)
    const denied = await fetch(`${baseUrl}/v1/providers`)
    assert.equal(denied.status, 401)

    const providers = await fetch(`${baseUrl}/v1/providers`, {
      headers: { 'x-astrabox-gateway-token': token },
    })
    assert.equal(providers.status, 200)
    assert.equal((await providers.json()).length, 16)

    const validation = await fetch(`${baseUrl}/v1/providers/telegram/validate`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-astrabox-gateway-token': token,
      },
      body: JSON.stringify({ config: {}, credentials: { token: 'telegram-token' } }),
    })
    assert.equal(validation.status, 200)
    assert.deepEqual(await validation.json(), {
      config: {},
      credentials: { token: 'telegram-token' },
    })
  } catch (error) {
    throw new Error(`${error.message}\ngateway output:\n${output}`, { cause: error })
  }
})
