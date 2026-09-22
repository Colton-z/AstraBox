import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { disableTypes } from 'image-size'

export const DISABLED_IMAGE_TYPES = Object.freeze(['heif', 'icns', 'jxl', 'jxl-stream'])
disableTypes(DISABLED_IMAGE_TYPES)

const DEFAULT_MANIFEST_URL = new URL(
  '../../astrabox/providers/channel_gateway_manifest.json',
  import.meta.url,
)

const isRecord = (value) =>
  value !== null && typeof value === 'object' && !Array.isArray(value)

function manifestPath() {
  const configured = String(process.env.ASTRABOX_CHANNEL_GATEWAY_MANIFEST || '').trim()
  return configured || fileURLToPath(DEFAULT_MANIFEST_URL)
}

function assertField(field, providerName) {
  if (!isRecord(field) || !String(field.key || '').trim()) {
    throw new Error(`provider ${providerName} has a field without a key`)
  }
  if (!['string', 'number', 'boolean', 'select'].includes(field.kind)) {
    throw new Error(`provider ${providerName} field ${field.key} has an unsupported kind`)
  }
  if (field.kind === 'select' && (!Array.isArray(field.options) || !field.options.length)) {
    throw new Error(`provider ${providerName} field ${field.key} has no select options`)
  }
}

export async function loadProviderCatalog(path = manifestPath()) {
  const parsed = JSON.parse(await readFile(path, 'utf8'))
  if (!isRecord(parsed) || parsed.version !== 1 || !Array.isArray(parsed.providers)) {
    throw new Error('channel provider manifest must have version=1 and a providers array')
  }
  const catalog = new Map()
  for (const raw of parsed.providers) {
    const name = String(raw?.name || '').trim()
    if (!name || catalog.has(name)) {
      throw new Error(`channel provider manifest has an empty or duplicate name: ${name}`)
    }
    if (!String(raw.adapter || '') || !String(raw.adapter_export || '')) {
      throw new Error(`provider ${name} has no adapter package/export`)
    }
    const configFields = raw.config_fields || []
    const credentialFields = raw.credential_fields || []
    if (!Array.isArray(configFields) || !Array.isArray(credentialFields)) {
      throw new Error(`provider ${name} fields must be arrays`)
    }
    for (const field of [...configFields, ...credentialFields]) assertField(field, name)
    const keys = [...configFields, ...credentialFields].map((field) => field.key)
    if (new Set(keys).size !== keys.length) {
      throw new Error(`provider ${name} has duplicate field keys`)
    }
    catalog.set(name, Object.freeze({ ...raw, name }))
  }
  if (!catalog.size) throw new Error('channel provider manifest is empty')
  return catalog
}

export function publicDescriptor(provider) {
  const credentialFields = provider.credential_fields.map((field) => ({
    ...field,
    secret: true,
  }))
  return {
    name: provider.name,
    label: provider.label,
    scene: `channel:${provider.name}`,
    setup_url: provider.setup_url,
    documentation_url: provider.documentation_url,
    callback_path: provider.callback_path || null,
    config_fields: provider.config_fields.map((field) => ({ ...field, secret: false })),
    credential_fields: credentialFields,
  }
}

function setPath(target, path, value) {
  const parts = path.split('.')
  let cursor = target
  for (const part of parts.slice(0, -1)) {
    if (!isRecord(cursor[part])) cursor[part] = {}
    cursor = cursor[part]
  }
  cursor[parts.at(-1)] = value
}

function getPath(target, path) {
  let cursor = target
  for (const part of path.split('.')) {
    if (!isRecord(cursor) || !(part in cursor)) return undefined
    cursor = cursor[part]
  }
  return cursor
}

function normalizeVisible(fields, input, label) {
  if (!isRecord(input)) throw new Error(`${label} must be an object`)
  const allowed = new Set(fields.map((field) => field.key))
  const unknown = Object.keys(input).filter((key) => !allowed.has(key))
  if (unknown.length) throw new Error(`${label} has unsupported fields: ${unknown.sort().join(', ')}`)

  const output = {}
  for (const field of fields) {
    let value = input[field.key]
    if ((value === undefined || value === null || value === '') && field.default !== undefined) {
      value = field.default
    }
    const missing = value === undefined || value === null || value === ''
    if (missing) {
      if (field.required) throw new Error(`${label}.${field.key} is required`)
      continue
    }
    if (field.kind === 'string' && typeof value !== 'string') {
      throw new Error(`${label}.${field.key} must be a string`)
    }
    if (field.kind === 'number' && (typeof value !== 'number' || !Number.isFinite(value))) {
      throw new Error(`${label}.${field.key} must be a number`)
    }
    if (field.kind === 'boolean' && typeof value !== 'boolean') {
      throw new Error(`${label}.${field.key} must be a boolean`)
    }
    if (field.kind === 'select' && !field.options.includes(value)) {
      throw new Error(`${label}.${field.key} must be one of ${field.options.join(', ')}`)
    }
    output[field.key] = typeof value === 'string' ? value.trim() : value
  }
  return output
}

const moduleCache = new Map()

async function adapterClass(provider) {
  if (!moduleCache.has(provider.adapter)) {
    moduleCache.set(provider.adapter, import(provider.adapter))
  }
  const module = await moduleCache.get(provider.adapter)
  const Adapter = module[provider.adapter_export]
  if (typeof Adapter !== 'function' || typeof Adapter.Config !== 'function') {
    throw new Error(
      `provider ${provider.name} adapter ${provider.adapter_export} has no executable Config schema`,
    )
  }
  return Adapter
}

export async function validateProvider(provider, configInput, credentialInput) {
  const config = normalizeVisible(provider.config_fields, configInput, 'config')
  const credentials = normalizeVisible(
    provider.credential_fields,
    credentialInput,
    'credentials',
  )
  const candidate = {}
  for (const [key, value] of Object.entries({ ...config, ...credentials })) {
    setPath(candidate, key, value)
  }
  for (const [key, value] of Object.entries(provider.fixed || {})) setPath(candidate, key, value)

  const Adapter = await adapterClass(provider)
  const normalized = Adapter.Config(candidate)
  const selectedConfig = {}
  const selectedCredentials = {}
  for (const field of provider.config_fields) {
    const value = getPath(normalized, field.key)
    if (value !== undefined) selectedConfig[field.key] = value
  }
  for (const field of provider.credential_fields) {
    const value = getPath(normalized, field.key)
    if (value !== undefined) selectedCredentials[field.key] = value
  }
  return {
    Adapter,
    adapterConfig: normalized,
    config: selectedConfig,
    credentials: selectedCredentials,
  }
}

export function redactError(error, credentials = {}) {
  let message = error instanceof Error ? error.message : String(error)
  const values = [credentials]
  while (values.length) {
    const value = values.pop()
    if (isRecord(value)) {
      values.push(...Object.values(value))
    } else if (typeof value === 'string' && value.length >= 4) {
      message = message.split(value).join('[redacted]')
    }
  }
  return message.replace(
    /((?:bearer|token|secret|password)\s*[=:]\s*)[^\s,;]+/gi,
    '$1[redacted]',
  )
}

export { isRecord }
