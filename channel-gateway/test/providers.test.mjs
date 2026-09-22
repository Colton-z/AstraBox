import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import imageSize from 'image-size'

import {
  DISABLED_IMAGE_TYPES,
  loadProviderCatalog,
  publicDescriptor,
  redactError,
  validateProvider,
} from '../src/providers.mjs'

const expectedProviders = [
  'telegram',
  'discord',
  'kook',
  'feishu',
  'lark',
  'dingtalk',
  'qq',
  'slack',
  'zulip',
  'mail',
  'line',
  'matrix',
  'satori',
  'wechat-official',
  'wecom',
  'whatsapp',
]

function sampleValue(field) {
  if (field.default !== undefined) return field.default
  if (field.kind === 'number') return 1
  if (field.kind === 'boolean') return true
  if (field.kind === 'select') return field.options[0]
  if (field.key === 'baseUrl' || field.key === 'endpoint') return 'https://chat.example.test'
  if (field.key.endsWith('.host') || field.key === 'host') return 'chat.example.test'
  if (field.key === 'email' || field.key === 'username' || field.key === 'selfId') {
    return 'bot@example.test'
  }
  return `${field.key.replaceAll('.', '-')}-value`
}

function sample(fields) {
  return Object.fromEntries(
    fields.filter((field) => field.required || field.default !== undefined).map((field) => [
      field.key,
      sampleValue(field),
    ]),
  )
}

test('the manifest exposes every product backed by the current official Satori adapters', async () => {
  const catalog = await loadProviderCatalog()
  assert.deepEqual([...catalog.keys()], expectedProviders)

  for (const provider of catalog.values()) {
    const descriptor = publicDescriptor(provider)
    assert.equal(descriptor.scene, `channel:${provider.name}`)
    if (!['mail', 'satori'].includes(provider.name)) assert.ok(descriptor.setup_url)
    assert.equal(descriptor.documentation_url, 'https://astrabox.ai/docs/channels')
    assert.equal(JSON.stringify(descriptor).includes('value-value'), false)
    assert.ok(descriptor.credential_fields.every((field) => field.secret === true))
  }
})

test('the provider catalog and installed dependencies exactly cover the pinned upstream catalog', async () => {
  const manifest = JSON.parse(await readFile(
    new URL('../../astrabox/providers/channel_gateway_manifest.json', import.meta.url),
    'utf8',
  ))
  const packageJson = JSON.parse(await readFile(
    new URL('../package.json', import.meta.url),
    'utf8',
  ))
  const official = [...manifest.upstream.packages].sort()
  const installed = Object.keys(packageJson.dependencies)
    .filter((name) => name.startsWith('@satorijs/adapter-'))
    .sort()
  const catalog = await loadProviderCatalog()
  const represented = [...new Set([...catalog.values()].map((provider) => provider.adapter))].sort()

  assert.equal(manifest.upstream.repository, 'https://github.com/satorijs/satori')
  assert.match(manifest.upstream.commit, /^[0-9a-f]{40}$/)
  assert.deepEqual(installed, official)
  assert.deepEqual(represented, official)
  assert.ok([...catalog.values()].every((provider) => official.includes(provider.adapter)))
})

test('every shipped provider sample reaches its adapter schema', async () => {
  const catalog = await loadProviderCatalog()
  for (const provider of catalog.values()) {
    const config = sample(provider.config_fields)
    const credentials = sample(provider.credential_fields)
    const result = await validateProvider(provider, config, credentials)
    assert.deepEqual(Object.keys(result.config).sort(), Object.keys(config).sort())
    assert.deepEqual(
      Object.keys(result.credentials).sort(),
      Object.keys(credentials).sort(),
    )
  }
})

test('validation rejects unknown fields and error redaction reaches nested secrets', async () => {
  const catalog = await loadProviderCatalog()
  const telegram = catalog.get('telegram')
  await assert.rejects(
    validateProvider(telegram, { invented: true }, { token: 'telegram-secret' }),
    /unsupported fields: invented/,
  )
  assert.equal(
    redactError(new Error('failed with nested-secret'), { nested: { token: 'nested-secret' } }),
    'failed with [redacted]',
  )
})

test('Telegram uses the official polling transport without a credential-bearing callback URL', async () => {
  const catalog = await loadProviderCatalog()
  const telegram = catalog.get('telegram')
  assert.equal(telegram.adapter, '@satorijs/adapter-telegram')
  assert.equal(telegram.adapter_export, 'TelegramBot')
  assert.deepEqual(telegram.fixed, { protocol: 'polling' })
  assert.equal(telegram.callback_path, null)
})

test('unbounded image parsers are disabled before official adapters load', () => {
  assert.deepEqual(DISABLED_IMAGE_TYPES, ['heif', 'icns', 'jxl', 'jxl-stream'])
  const icns = Buffer.alloc(16)
  icns.write('icns', 0)
  icns.writeUInt32BE(icns.length, 4)
  assert.throws(() => imageSize(icns), /disabled file type: icns/)
})
