/**
 * E2E: a session stream that died without ending must not be the reason the
 * next answer never appears.
 *
 * The journey is a suspended laptop, a changed IP, a proxy that drops a socket
 * without a FIN ever reaching the browser. The tab stays open and the response
 * behind it stops delivering bytes — it does not close, and it does not error.
 * The user comes back, types the next message, and waits. The platform accepts
 * the input, runs the response and records the answer; the page is still
 * reading a socket nobody is writing to, so the transcript never grows.
 * Nothing says so: the pill reads READY and the composer is enabled, because
 * both are driven by the session-detail poll rather than by the stream.
 * Reload is the only way out, and nothing asks for one.
 *
 * WHAT HAS TO END THE SILENCE, read rather than run: the page reads its stream
 * through one `SessionFollower` (`frontend/src/session/sessionFollower.ts`),
 * which reconnects whenever a request ends or fails, and a request that never
 * ends never lets it. The one byte clock on that path is `withStreamLiveness`
 * (`frontend/src/session/streamLiveness.ts`), which the follower's transport
 * wraps around every stream response and which ends a body that has gone
 * `STREAM_SILENCE_LIMIT_MS` (45s) without a byte. This spec is the
 * product-level statement of that rule: the silence has to become a
 * disconnect the page resumes from, on the same page, with no reload.
 *
 * WHAT THE FAULT IS, EXACTLY. `installDeadStream` keeps reading the backend and
 * stops handing chunks to the application: the server is never backpressured,
 * so the response runs and ends normally, while the AI SDK's read never
 * resolves. When the backend ends that request the application is not told
 * either — a socket that ate the bytes ate the FIN with them. That is the whole
 * point: it leaves the page no observable event to react to, which is what
 * separates this from the faults that already heal. Status, headers and bytes
 * are the backend's own; nothing is fabricated.
 *
 * WHY THE SILENCE IS COUNTED IN KEEPALIVES: the backend writes `: keepalive`
 * into an idle stream after every 15 seconds of silence (`_KEEPALIVE_S` in
 * `astrabox/api/routes/turns.py`). Counting the ones the application never saw
 * is what makes a lane-sized wait mean a lidded laptop: the wire was healthy
 * throughout and the page missed every liveness signal on it. A timeout
 * waiting for them is a finding of its own — the server stopped keepaliving
 * an idle stream.
 *
 * WHY AN ANCHOR REPLY COMES FIRST: it is the same verdict made a minute
 * earlier on the same page, so a later red cannot be blamed on how the answer
 * is looked for — and it is what gives the page a resume cursor at all,
 * without which "the page resumed from where it actually got to" has nothing
 * to compare.
 *
 * Engine-independent: two tool-free replies, no tool cards, no permission modes,
 * no interactions, and no SDK frame vocabulary beyond the platform's own SSE
 * framing. Whatever the engine answers is what must render.
 */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi, messageText, type InputReceipt } from '../fixtures/astraApi';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import {
  expectComposerEnabled,
  expectPromptDelivered,
  openSessionView,
  sendPrompt,
  startPromptDelivery,
} from '../fixtures/sessionPage';

// The anchor reply establishes that this page's live channel works at all.
const ANCHOR_REPLY_MS = parseTimeoutEnv('ASTRABOX_E2E_DEAD_STREAM_ANCHOR_MS', 60_000);
// Headroom over SILENCE_KEEPALIVES x 15s, so a late first keepalive is not a failure.
const SILENCE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_DEAD_STREAM_SILENCE_MS', 45_000);
// How long the platform may take to answer the message sent into the dead page.
const ANSWER_MS = parseTimeoutEnv('ASTRABOX_E2E_DEAD_STREAM_ANSWER_MS', 60_000);
// How long the page may take to put an already-durable answer on screen. This is
// the judgement window: it starts when the answer becomes durable.
const RECOVERY_MS = parseTimeoutEnv('ASTRABOX_E2E_DEAD_STREAM_RECOVERY_MS', 45_000);
// How long the settled page may take to hold exactly one open stream request.
const SUBSCRIPTION_MS = parseTimeoutEnv('ASTRABOX_E2E_DEAD_STREAM_SUBSCRIPTION_MS', 20_000);

/**
 * How many server keepalives must pass unseen before the user acts.
 *
 * One at the server's 15s idle period is enough to prove the SHAPE — a healthy
 * wire the application is not reading — which is all this spec needs, because
 * the window a fix actually gets is this silence plus the send, the response
 * and the whole recovery budget. A spec that wanted to pin a staleness
 * THRESHOLD would raise this; that product rule is not this spec's subject,
 * and buying it here costs 15 seconds of a 180s wall apiece.
 */
const SILENCE_KEEPALIVES = countEnv('ASTRABOX_E2E_DEAD_STREAM_KEEPALIVES', 1);

// Long enough that two replies cannot share it by accident, short enough that a
// one-token answer supplies it.
const MIN_NEEDLE_CHARS = 12;
// The head of a reply, as it would read on screen. Longer is less repeatable.
const NEEDLE_CHARS = 48;

// A FAILED response renders its error INTO the transcript as an assistant
// message (`chat:result.turn_failed*`), so "a new assistant message exists" is
// satisfied by exactly the failure this spec must not pass on.
const TURN_ERROR = /This turn failed|本轮执行失败|API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i;

const ANCHOR_PROMPT =
  'Reply with exactly ANCHORTOKEN4B1 and nothing else. Do not use any tool.';
const WAKE_PROMPT =
  'Reply with exactly CONTINUITYTOKEN9F2 and nothing else. Do not use any tool.';

/** A positive integer from the environment, or the fallback. Not a duration. */
function countEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

/** One GET the BROWSER made on the session stream, and the cursor it asked from. */
interface StreamOpen {
  url: string;
  afterSeq: number | null;
  at: number;
}

/** One session stream request the browser opened, and its real bytes. */
interface DeadConnection {
  id: number;
  afterSeq: number | null;
  status: number;
  contentType: string;
  /** Every byte read from the backend on this response. */
  raw: string;
  /** The bytes enqueued for the application's parser. */
  delivered: string;
  /** `raw.length` when the socket was killed; 0 on a connection never killed. */
  silenceStart: number;
  blackholed: boolean;
  /** The BACKEND ended this response — which a killed socket never reports on. */
  upstreamClosed: boolean;
  /** The APPLICATION saw this response end, cleanly or with an error. */
  closed: boolean;
}

type DeadStreamWindow = Window & typeof globalThis & {
  __astraboxDeadStream: {
    connections: DeadConnection[];
    blackhole: (id: number) => { delivered: number; raw: number; connections: number };
    withheldKeepalives: (id: number) => number;
    disarm: () => void;
  };
};

/**
 * Make one session stream request die without ending.
 *
 * The follower's transport calls `globalThis.fetch` and wraps what it returns
 * in `withStreamLiveness`, so wrapping `window.fetch` before the app's first
 * script puts this between the backend and the page's byte clock. Status,
 * statusText and headers are the backend's own and the body re-emits the
 * backend's own chunks.
 *
 * A BLACKHOLED connection keeps reading the backend — its keepalives really
 * arrive, which is how the silence is measured — and simply stops enqueueing.
 * It never errors, because an error is the case that already heals
 * (`live-stream-reconnect-...` covers it). It also never closes, INCLUDING when
 * the backend ends the request: a socket that swallowed the bytes swallowed the
 * FIN with them. That last part matters more than it looks. One stream request
 * ends after a response's `finish`, so if the application were told the
 * request ended it would reconnect by itself at the end of the very response
 * whose reply it failed to show — and that page would satisfy a naive "the
 * answer eventually rendered" assertion. Withholding the close removes that
 * path instead of asserting around it.
 *
 * The reading happens INSIDE one `pull`. A `pull` that returns without
 * enqueueing is not called again, so a per-chunk withhold would stop draining at
 * the first withheld chunk and the connection would look idle rather than
 * silent. Unstalled reads deliver one chunk per `pull`.
 *
 * Only `GET .../sessions/{id}/stream` is touched. The `inputs` POST and every
 * history GET pass through untouched, which is what keeps the platform half of
 * this journey real.
 */
async function installDeadStream(page: Page, sessionId: string): Promise<void> {
  await page.addInitScript(({ streamPath }) => {
    const originalFetch = window.fetch.bind(window);
    const aborts = new Map<number, () => void>();
    let armed = true;
    const state: DeadStreamWindow['__astraboxDeadStream'] = {
      connections: [],
      blackhole: (id) => {
        const record = state.connections[id];
        if (!record) throw new Error(`no session stream request ${id}`);
        if (record.closed) throw new Error(`stream request ${id} is already closed`);
        if (record.blackholed) throw new Error(`stream request ${id} is already dead`);
        const tail = record.raw.split(/\r?\n\r?\n/).at(-1) ?? '';
        if (tail.trim()) {
          throw new Error(
            `stream request ${id} is mid-record; killing it there would split an SSE event`,
          );
        }
        record.silenceStart = record.raw.length;
        record.blackholed = true;
        return {
          delivered: record.delivered.length,
          raw: record.raw.length,
          connections: state.connections.length,
        };
      },
      withheldKeepalives: (id) => {
        const record = state.connections[id];
        if (!record) throw new Error(`no session stream request ${id}`);
        if (!record.blackholed) throw new Error(`stream request ${id} is not dead`);
        // An SSE comment record is `: keepalive\n\n`; a business frame starts
        // `data:`, so a prefix test separates them without parsing either.
        return record.raw
          .slice(record.silenceStart)
          .split(/\r?\n\r?\n/)
          .filter((block) => block.startsWith(':')).length;
      },
      disarm: () => {
        armed = false;
        window.fetch = originalFetch;
        for (const record of state.connections) record.blackholed = false;
        for (const abort of aborts.values()) abort();
        aborts.clear();
      },
    };
    (window as DeadStreamWindow).__astraboxDeadStream = state;
    window.fetch = async (input, init) => {
      const request = input instanceof Request ? input : null;
      const method = String(init?.method ?? request?.method ?? 'GET').toUpperCase();
      const url = new URL(request?.url ?? String(input), window.location.href);
      const response = await originalFetch(input, init);
      if (!armed || method !== 'GET' || url.pathname !== streamPath) {
        return response;
      }
      const rawCursor = url.searchParams.get('after_seq');
      const record: DeadConnection = {
        id: state.connections.length,
        afterSeq: rawCursor === null ? null : Number(rawCursor),
        status: response.status,
        contentType: response.headers.get('content-type') || '',
        raw: '',
        delivered: '',
        silenceStart: 0,
        blackholed: false,
        upstreamClosed: false,
        closed: false,
      };
      state.connections.push(record);
      if (
        response.status !== 200
        || !record.contentType.includes('text/event-stream')
        || !response.body
      ) {
        record.upstreamClosed = true;
        record.closed = true;
        return response;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      aborts.set(record.id, () => { void reader.cancel('E2E dead stream disarm'); });
      const body = new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            for (;;) {
              const chunk = await reader.read();
              if (chunk.done) {
                record.upstreamClosed = true;
                if (record.blackholed) {
                  // The dead socket's silence includes the end of the response.
                  // This promise is meant never to settle: the application's
                  // read stays pending exactly as it would on a socket whose
                  // FIN never arrived.
                  await new Promise(() => {});
                  return;
                }
                record.closed = true;
                aborts.delete(record.id);
                controller.close();
                return;
              }
              const text = decoder.decode(chunk.value, { stream: true });
              record.raw += text;
              if (record.blackholed) continue;
              record.delivered += text;
              controller.enqueue(chunk.value);
              return;
            }
          } catch (error) {
            if (record.closed) return;
            record.closed = true;
            aborts.delete(record.id);
            controller.error(error);
          }
        },
        async cancel(reason) {
          record.closed = true;
          aborts.delete(record.id);
          await reader.cancel(reason);
        },
      });
      return new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    };
  }, { streamPath: apiPath(`/sessions/${sessionId}/stream`) });
}

async function streamConnections(page: Page): Promise<DeadConnection[]> {
  return page.evaluate(() => (window as DeadStreamWindow).__astraboxDeadStream.connections);
}

/**
 * The resume cursor the application ACTUALLY consumed on these connections.
 *
 * The follower takes each `data-resume-cursor` part it parses as the position
 * its next request starts from (`after_seq`). Reading the same value out of
 * the bytes that reached the parser is therefore the independent statement of
 * where a correct reconnect must start — computed from the wire rather than
 * from the page's own state, so it cannot agree with a broken page by
 * construction. The stream's cursors only grow, so the highest one is the last.
 */
function cursorFromDelivered(delivered: string): number | null {
  let highest: number | null = null;
  for (const line of delivered.split(/\r?\n/)) {
    if (!line.startsWith('data:')) continue;
    const payload = line.slice('data:'.length).trim();
    if (!payload || payload === '[DONE]') continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload);
    } catch {
      // A truncated trailing record is not a cursor; the next read completes it.
      continue;
    }
    if (!parsed || typeof parsed !== 'object') continue;
    const frame = parsed as { type?: unknown; data?: { seq?: unknown } };
    if (frame.type !== 'data-resume-cursor') continue;
    const seq = frame.data?.seq;
    if (typeof seq !== 'number' || !Number.isInteger(seq) || seq < 0) continue;
    if (highest === null || seq > highest) highest = seq;
  }
  return highest;
}

/** Rendered reply parts only; reasoning and progress labels are not the answer. */
async function assistantTranscript(page: Page): Promise<string> {
  return (await page.getByTestId('assistant-text').allInnerTexts()).join('\n');
}

/**
 * Rendered prose with layout and markdown decoration removed.
 *
 * The same text is not the same string on both sides of the stream/settled
 * boundary — a half-arrived `**` is literal mid-stream and gone once the
 * emphasis closes, and line wrapping moves with the panel. Normalizing keeps the
 * comparison about content.
 */
function normalizeRendered(text: string): string {
  return text.replace(/\s+/g, '').replace(/[*_`#>|~\-–—·•]/g, '');
}

/** The identifying head of a durable reply, as it would read on screen. */
function renderedNeedle(text: string, label: string): string {
  const normalized = normalizeRendered(text);
  expect(
    normalized.length,
    `${label} must be long enough to identify on screen (>= ${MIN_NEEDLE_CHARS} chars): ` +
      JSON.stringify(text.slice(0, 200)),
  ).toBeGreaterThanOrEqual(MIN_NEEDLE_CHARS);
  return normalized.slice(0, NEEDLE_CHARS);
}

/** How many times `needle` occurs in `haystack`. Overlaps are not possible here. */
function occurrences(haystack: string, needle: string): number {
  let count = 0;
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) return count;
    count += 1;
    from = at + needle.length;
  }
}

const sessions = trackSessions();

test('a stream that dies without ending still delivers the next answer without a reload', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);

  // Setup is API-only: none of it is the subject, and a conversation opened from
  // the page would make the first stream request harder to identify.
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  // Every GET the BROWSER makes on this session's stream, with the cursor it
  // asked from. Registered before the first navigation so the console's first
  // channel open is captured too. `page.on('request')` reads the page's own
  // network without touching the body the page is reading — the second,
  // independent oracle for "did it come back, and from where".
  const streamOpens: StreamOpen[] = [];
  const streamPath = apiPath(`/sessions/${sessionId}/stream`);
  page.on('request', (req) => {
    if (req.method() !== 'GET' || new URL(req.url()).pathname !== streamPath) return;
    const raw = new URL(req.url()).searchParams.get('after_seq');
    const parsed = raw === null ? Number.NaN : Number(raw);
    streamOpens.push({
      url: req.url(),
      afterSeq: Number.isFinite(parsed) ? parsed : null,
      at: Date.now(),
    });
  });

  await api.waitForSessionReady(sessionId);
  await installDeadStream(page, sessionId);

  try {
    await openSessionView(page, sessionId);
    await expectComposerEnabled(page);
    const pageInstance = await page.evaluate(() => performance.timeOrigin);
    const statusPill = page.getByTestId('run-view').getByTestId('status-pill').first();

    // ── ANCHOR ─────────────────────────────────────────────────────────────
    // The verdict this spec will reach, made a minute earlier on the same page:
    // the live channel works and the transcript renders recorded replies. It is
    // also what gives the page a real resume cursor to be judged against.
    const anchorBefore = await api.assistantCount(sessionId);
    await sendPrompt(page, sessionId, ANCHOR_PROMPT);
    // Matched on text, not on count: an assistant record exists from its
    // first block, and an engine that thinks before it speaks publishes one
    // with no text for a while.
    const anchorReply = await api.waitForAssistantMessageMatching(
      sessionId,
      anchorBefore,
      (message) => normalizeRendered(messageText(message)).length >= MIN_NEEDLE_CHARS,
      ANCHOR_REPLY_MS,
    );
    const anchorText = messageText(anchorReply);
    expect(anchorText, 'the anchor message must be answered, not record a failure').not.toMatch(TURN_ERROR);
    const anchorNeedle = renderedNeedle(anchorText, 'the anchor reply');
    await expect
      .poll(async () => normalizeRendered(await assistantTranscript(page)).includes(anchorNeedle), {
        timeout: RECOVERY_MS,
        message: 'the page must render the anchor reply — the live channel is working here',
      })
      .toBe(true);

    // ── THE PAGE SETTLES ONTO ITS IDLE STREAM REQUEST ──────────────────────
    // The anchor's `finish` ends the request it streamed on and the page opens
    // the next one, which waits idle. That one is the subject.
    await api.waitForSessionReady(sessionId);
    await expect(statusPill).toHaveAttribute('data-state', 'READY', { timeout: SUBSCRIPTION_MS });
    await expectComposerEnabled(page);
    await expect
      .poll(async () => (await streamConnections(page)).filter((c) => !c.closed).length, {
        timeout: SUBSCRIPTION_MS,
        message: 'the settled page must hold exactly one open session stream request',
      })
      .toBe(1);
    // Where the page actually got to. Everything the reconnect is judged on
    // hangs off this number, so it is read before the fault and asserted to
    // exist: a null here would mean the page never consumed a resume cursor and
    // the cursor half of the verdict would be vacuous rather than passing.
    const connectionsBeforeDeath = await streamConnections(page);
    const deliveredCursor = cursorFromDelivered(
      connectionsBeforeDeath.map((connection) => connection.delivered).join(''),
    );
    expect(
      deliveredCursor,
      'the page must have consumed a resume cursor before the socket dies, or "it resumed from ' +
        'where it got to" has nothing to compare against',
    ).not.toBeNull();
    const cursorAtDeath = deliveredCursor as number;

    // ── THE SOCKET DIES ────────────────────────────────────────────────────
    // From here the backend keeps writing into this socket and the wrapper keeps
    // draining it; the application receives nothing. Nothing in the read path
    // settles the follower's pending read, so only a clock over its bytes can
    // end it.
    // Chosen and killed in the same breath: a page that has just settled can
    // still rotate its stream request once, and an id read a moment earlier may
    // name a connection that has closed since. Only a lone open connection is
    // the subject.
    const killDeadline = Date.now() + SUBSCRIPTION_MS;
    let killed: { id: number; delivered: number; raw: number; connections: number } | null = null;
    while (killed === null) {
      killed = await page.evaluate(() => {
        const deadStream = (window as DeadStreamWindow).__astraboxDeadStream;
        const open = deadStream.connections.filter((connection) => !connection.closed);
        if (open.length !== 1) return null;
        return { id: open[0].id, ...deadStream.blackhole(open[0].id) };
      });
      if (killed === null) {
        expect(
          Date.now() <= killDeadline,
          'the settled page must hold exactly one open session stream request to kill',
        ).toBe(true);
        await page.waitForTimeout(250);
      }
    }
    const atDeath = killed;
    const idle = (await streamConnections(page))[atDeath.id];
    expect(idle.status, 'the subject connection must be a real successful response').toBe(200);
    expect(idle.contentType).toContain('text/event-stream');
    // The wrapper's own invariant, asserted so the divergence measured below is
    // attributable to the fault and not to a backlog that predated it: while a
    // connection is honest, every byte read is enqueued in the same breath.
    expect(atDeath.delivered, 'the silence must begin from a fully caught-up connection')
      .toBe(atDeath.raw);
    const opensBeforeDeath = streamOpens.length;

    // Spend the silence in the server's units. A timeout here is its own
    // finding: the backend stopped keepaliving an idle stream.
    await expect
      .poll(
        () => page.evaluate(
          (id) => (window as DeadStreamWindow).__astraboxDeadStream.withheldKeepalives(id),
          idle.id,
        ),
        {
          timeout: SILENCE_TIMEOUT_MS,
          message:
            `the backend must write at least ${SILENCE_KEEPALIVES} keepalive(s) into the dead ` +
            'connection — that is the proof the wire stayed healthy while the page read nothing',
        },
      )
      .toBeGreaterThanOrEqual(SILENCE_KEEPALIVES);

    // The reproduction is a silent death, not a failure: real liveness signals
    // arrived, the application got none of them, nothing closed, nothing raised.
    const dead = (await streamConnections(page))[idle.id];
    expect(dead.closed, 'the dead connection must still be open to the app — this is not the error path')
      .toBe(false);
    expect(
      dead.upstreamClosed,
      'the backend must not have ended this response on its own; if it did, the silence under ' +
        'test is the server hanging up rather than a socket that stopped delivering',
    ).toBe(false);
    expect(dead.delivered.length, 'not one byte may reach the application during the silence')
      .toBe(atDeath.delivered);
    // The page's byte clock needs STREAM_SILENCE_LIMIT_MS of silence, and the
    // first withheld keepalive arrives well inside it, so nothing has had a
    // reason to open a second channel yet.
    expect(
      (await streamConnections(page)).length,
      'nothing may open a second channel before the silence limit',
    ).toBe(atDeath.connections);
    // And the page looks entirely normal while its channel is dead, which is why
    // the user has no reason to do anything except keep typing.
    await expect(statusPill).toHaveAttribute('data-state', 'READY');
    await expectComposerEnabled(page);

    // ── THE USER COMES BACK TO THE TAB ─────────────────────────────────────
    // The two signals a returning tab gives a page. This is a stimulus, not a
    // claim: a page that heals here is a pass, so the open count around it is
    // recorded and not asserted. The send below has to be enough on its own,
    // because a user who clicks into a tab and types has given the page every
    // signal it will get.
    const visible = await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => 'visible',
      });
      document.dispatchEvent(new Event('visibilitychange'));
      return document.visibilityState;
    });
    expect(visible, 'the return to the tab must be observable to the page').toBe('visible');
    const opensAfterVisibility = streamOpens.length;

    // ── THE USER SENDS THE NEXT MESSAGE ────────────────────────────────────
    const usersBefore = await page.getByTestId('user-message').count();
    const transcriptBeforeSend = normalizeRendered(await assistantTranscript(page));
    // The delivery pair, rather than `sendPrompt`, because the receipt the
    // platform acknowledges this input by is part of the evidence: its input id
    // is what the response below names, which makes it indisputably the answer
    // to the message typed into the dead page. `startPromptDelivery` fails
    // loudly if the client message id is missing, and `expectPromptDelivered`
    // if the POST is not a 200.
    const delivery = await startPromptDelivery(page, sessionId, WAKE_PROMPT);
    const receipt = ((await (await expectPromptDelivered(delivery)).json()) as { data: InputReceipt }).data;
    expect(receipt.client_message_id, 'the receipt must be for the message the page sent')
      .toBe(delivery.clientMessageId);

    // ── SERVER TRUTH, BEFORE THE PAGE IS JUDGED ────────────────────────────
    // Read through the product's own messages API — what the user's page would
    // read — so this half stands whatever the page is doing.
    const answer = await api.waitForReplyTo(sessionId, receipt.input_id, { timeoutMs: ANSWER_MS });
    const durableAnswer = messageText(answer);
    expect(answer.metadata?.outcome, 'the platform must complete the response, not fail it').toBe('completed');
    expect(durableAnswer, 'the platform must answer, not record a failed response').not.toMatch(TURN_ERROR);
    await api.waitForSessionReady(sessionId, ANSWER_MS);
    const needle = renderedNeedle(durableAnswer, 'the answer to the message sent into the dead page');
    // Anti-vacuity: the verdict must be able to tell this answer apart from what
    // was already on screen before the send.
    expect(
      transcriptBeforeSend.includes(needle),
      'the new answer must not be identifiable by text the page already showed',
    ).toBe(false);

    // ── THE VERDICT: does the user ever see it? ────────────────────────────
    // The judgement window starts here, with the answer already durable.
    await expect
      .poll(async () => normalizeRendered(await assistantTranscript(page)).includes(needle), {
        timeout: RECOVERY_MS,
        message:
          'the page must render the answer to the message the user just sent. The platform has ' +
          'it; the page is still holding a stream request that stopped delivering bytes and never ' +
          'ended, so the user is left staring at their own message under a READY pill with no ' +
          'reason to suspect a reload is the only way out',
      })
      .toBe(true);

    // The user's message did not rot: it is on the page once, and nothing under
    // it says it was not delivered (`UserMessageStatus` renders
    // `user-message-failure` for a send the page or the platform refused).
    await expect(page.getByTestId('user-message')).toHaveCount(usersBefore + 1);
    await expect(page.getByTestId('user-message-failure')).toHaveCount(0);

    // ── EVIDENCE: it came back on a new channel, from where it got to ──────
    // A page that read history once would render this answer and leave the
    // next message just as dead; the page must hold a live channel again.
    await expect
      .poll(() => streamOpens.length - opensBeforeDeath, {
        timeout: RECOVERY_MS,
        message:
          'no stream request after the stream went silent while a response to the page\'s own ' +
          'message was running — re-establishing the live channel is what heals, a one-off ' +
          'history refetch is not',
      })
      .toBeGreaterThan(0);
    const reopens = streamOpens.slice(opensBeforeDeath);
    const reopenCursors = reopens
      .map((open) => open.afterSeq)
      .filter((seq): seq is number => seq !== null && seq >= 0);
    expect(
      reopenCursors.length,
      'the reopened stream request must carry a cursor (after_seq) — one without replays the ' +
        'whole session through a fresh parser',
    ).toBe(reopens.length);
    // Not merely "some after_seq": `after_seq=0` IS a restart from the head. The
    // floor is the cursor the page ACTUALLY consumed, parsed out of the bytes
    // that reached its parser. Anything below it is the page going back for
    // parts it had already painted; at or past it is the only honest resume,
    // and a page that reads history again first legitimately lands past it,
    // which is why this is a floor and not an equality.
    expect(
      Math.min(...reopenCursors),
      'every stream request opened after the death must resume AT OR PAST the cursor the page ' +
        'had actually consumed, not rewind behind it',
    ).toBeGreaterThanOrEqual(cursorAtDeath);

    // No duplication: a resume from the head repaints the conversation on top of
    // itself. Both replies must read once. (If an engine repeats its own opening
    // clause verbatim within one answer, this fails with the transcript in the
    // message — rare, and diagnosable from the attachment below.)
    const settled = normalizeRendered(await assistantTranscript(page));
    expect(occurrences(settled, needle), 'the new answer must be painted once, not replayed').toBe(1);
    expect(occurrences(settled, anchorNeedle), 'the anchor reply must not be repainted').toBe(1);

    // ── EVIDENCE: the page healed itself, not the test ─────────────────────
    expect(
      await page.evaluate(() => performance.timeOrigin),
      'no page reload may stand in for the page noticing — including one a release ' +
        'check could have triggered on the same visibilitychange',
    ).toBe(pageInstance);
    expect(new URL(page.url()).pathname.endsWith(`/sessions/${sessionId}`)).toBe(true);

    // The honesty guard on the whole case: if the answer appeared, it came from a
    // new connection and not from the harness leaking bytes through the dead one.
    const final = await streamConnections(page);
    expect(
      final[idle.id].delivered.length,
      'the dead connection must still have delivered nothing; anything else means the fault was ' +
        'lifted and this spec proved something other than what it claims',
    ).toBe(atDeath.delivered);

    // ── The page is usable, not merely recovered ───────────────────────────
    await expect(statusPill).toHaveAttribute('data-state', 'READY');
    await expect(statusPill).toHaveAttribute('data-pulse', 'false');
    await expectComposerEnabled(page);

    test.info().annotations.push({
      type: 'e2e_dead_stream_report',
      description: JSON.stringify({
        clientMessageId: delivery.clientMessageId,
        cursorAtDeath,
        opensBeforeDeath,
        opensAfterVisibility,
        reopens,
        connections: final.map((connection) => ({
          id: connection.id,
          afterSeq: connection.afterSeq,
          raw: connection.raw.length,
          delivered: connection.delivered.length,
          blackholed: connection.blackholed,
          upstreamClosed: connection.upstreamClosed,
          closed: connection.closed,
        })),
        durableAnswer: durableAnswer.slice(0, 200),
      }),
    });
  } finally {
    // Page-local only. The session is `trackSessions`' afterEach, which keeps the
    // scene on failure. Disarming releases the backend response the dead
    // connection is still holding, so a failed assertion does not strand it.
    if (!page.isClosed()) {
      await page.evaluate(() => (window as DeadStreamWindow).__astraboxDeadStream?.disarm());
    }
  }
});
