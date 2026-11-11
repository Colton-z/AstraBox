// A dsh-superagent Source in its own package, for the e2e to-do service
// (tests/e2e-ui/fixtures/todo_service.py). It follows the bundle's cookbook
// (docs/008-cookbook.md, "Add a Source") and imports the bundle's Source
// interface from `dsh-superagent/sources`.
//
// The service is read and answered with the token in TODO_TOKEN, which the
// Assistant's Credential Vault supplies: the process holds a placeholder that
// the sandbox's outbound proxy replaces in requests to the service.
import { health, key, record, sourceContext, threadKey } from 'dsh-superagent/sources'

const KIND = 'todo'
const CURSOR = record('todo-cursor')

/** A schema the Source registry can call: `parse` throws, `safeParse` reports. */
function schema(parse) {
  return {
    parse,
    safeParse(value) {
      try { return { success: true, data: parse(value) } } catch (error) { return { success: false, error } }
    },
  }
}
const ConfigSchema = schema((value) => {
  const config = value ?? {}
  const baseUrl = typeof config.baseUrl === 'string' ? config.baseUrl.trim().replace(/\/+$/, '') : ''
  if (baseUrl && !/^https?:\/\/\S+$/.test(baseUrl)) throw new Error('baseUrl must be an http(s) URL')
  const pollSeconds = config.pollSeconds === undefined ? 5 : Number(config.pollSeconds)
  if (!Number.isInteger(pollSeconds) || pollSeconds < 2 || pollSeconds > 3600) throw new Error('pollSeconds must be 2–3600')
  return { baseUrl, pollSeconds }
})
const PolicySchema = schema((value) => {
  const allowed = Array.isArray(value?.allowed) ? value.allowed : []
  return {
    allowed: allowed.map((entry) => {
      if (!entry || typeof entry.id !== 'string' || !entry.id) throw new Error('allowed[].id is required')
      return { id: entry.id, ...(typeof entry.name === 'string' ? { name: entry.name } : {}) }
    }),
  }
})

async function call(baseUrl, path, init = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${process.env.TODO_TOKEN ?? ''}`, 'Content-Type': 'application/json', ...init.headers },
  })
  return response
}

/** The Trigger of one to-do event; the agent's own comments produce none. */
function todoTrigger(event, me, baseUrl) {
  if (event.author === me) return undefined
  return {
    id: key(KIND, event.id),
    source: KIND,
    threads: [{ source: KIND, id: event.item }],
    actor: { source: KIND, id: event.author },
    human: true,
    ...(event.text.includes(`@${me}`) ? { addressed: 'mention' } : {}),
    at: event.at,
    text: event.text,
    // The address holds everything a reply needs, as the cookbook asks: the service and the item.
    channel: { source: KIND, thread: `${KIND}:${event.item}`, address: { base: baseUrl, item: event.item, url: event.url } },
    url: event.url,
    meta: { item: event.item },
  }
}

export const todoKind = {
  kind: KIND,
  title: { zh: '待办服务', en: 'To-do service' },
  description: { zh: '接收待办服务里的事项和评论。', en: 'Receives the items and comments of the to-do service.' },
  schema: ConfigSchema,
  fields: [
    { key: 'baseUrl', type: 'text', label: { zh: '服务地址', en: 'Service URL' } },
    { key: 'pollSeconds', type: 'number', advanced: true, min: 2, max: 3600, label: { zh: '轮询间隔 / 秒', en: 'Polling interval / seconds' } },
  ],
  health: ['todo'],
  scope: () => ({ listened: true, observed: false }),
  channel: {
    async send(_host, message) {
      const { base, item } = message.channel.address
      const response = await call(base, `/api/items/${encodeURIComponent(item)}/comments`, {
        method: 'POST', body: JSON.stringify({ key: message.id, text: message.text }),
      })
      if (response.status === 404) return 'gone'
      if (!response.ok) throw new Error(`the to-do service refused the reply: HTTP ${response.status}`)
      return 'sent'
    },
    link: (address) => ({ url: address.url ?? '', label: { zh: `待办 ${address.item}`, en: `To-do ${address.item}` } }),
  },
  policy: {
    schema: PolicySchema,
    fields: [{ key: 'allowed', type: 'rows', label: { zh: '能发起工作的人', en: 'People who may originate work' }, columns: [
      { key: 'id', type: 'text', label: { zh: '账号', en: 'Account' } },
      { key: 'name', type: 'text', label: { zh: '名字', en: 'Name' }, optional: true }] }],
    explicit: { zh: '名单里的人在事项或评论里 @ 智能体。', en: 'Someone on the list mentions the agent in an item or a comment.' },
  },
}

async function poll(ctx, config, signal) {
  const after = ctx.state.get(CURSOR) ?? 0
  const response = await call(config.baseUrl, `/api/events?after=${after}`, { signal })
  if (!response.ok) throw new Error(`the to-do service refused the read: HTTP ${response.status}`)
  const page = await response.json()
  for (const event of page.events) {
    if (signal.aborted) throw new Error('the to-do Source stopped during the read')
    const trigger = todoTrigger(event, page.me, config.baseUrl)
    if (!trigger) continue
    await ctx.receive(trigger)
    for (const ref of trigger.threads) ctx.threads.describe(threadKey(ref), { label: event.title, url: event.url })
  }
  ctx.state.set(CURSOR, page.next)
  return page.events.length
}

async function watch(ctx, signal) {
  await ctx.ready
  if (!ctx.configured()) { ctx.report('todo', health('paused', 'setupPending')); return }
  let failures = 0
  while (!signal.aborted) {
    const config = ctx.config(KIND)
    if (!config || !config.baseUrl) ctx.report('todo', health('paused', 'sourceOff'))
    else {
      const at = new Date().toISOString()
      try {
        const read = await poll(ctx, config, signal)
        failures = 0
        ctx.report('todo', { state: 'ready', detail: `${read} events`, at })
      } catch (error) {
        failures += 1
        if (!signal.aborted && failures >= 3) {
          ctx.report('todo', health('error', 'pollFailures', { count: String(failures), error: String(error) }))
        }
      }
    }
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, (config?.pollSeconds ?? 5) * 1000)
      signal.addEventListener('abort', () => { clearTimeout(timer); resolve() }, { once: true })
    })
  }
}

export const name = 'astrabox-e2e-todo-source'
export const inject = ['orchestrator']
export function apply(ctx) {
  ctx.effect(() => ctx.orchestrator.sources.register(todoKind), 'todo: source kind')
  ctx.effect(() => {
    const source = sourceContext(ctx.orchestrator, todoKind)
    const controller = new AbortController()
    const running = watch(source, controller.signal)
      .catch((error) => { source.report('todo', { state: 'error', detail: String(error) }) })
    return async () => { controller.abort(); await running }
  })
}
