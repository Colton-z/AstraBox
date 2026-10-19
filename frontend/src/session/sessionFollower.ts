import { Chat } from '@ai-sdk/react';
import { DefaultChatTransport, JSONParseError, TypeValidationError } from 'ai';
import type { UIMessage, UIMessageChunk } from 'ai';
import { buildSessionStreamUrl } from '../api';
import type { PendingInteraction } from '../types';
import { lastOpenResponse, responseMetadata, retractMessageParts, type InputFailure, type ResponseMetadata } from './sessionMessages';
import { withStreamLiveness } from './streamLiveness';
import { consoleFetch } from '../consoleTransport';

/**
 * Follow one session's stream (`GET /sessions/{id}/stream?after_seq=`).
 *
 * The stream delivers AI SDK UI message chunks once each, in order: what
 * happened after the cursor, then live. One HTTP response ends after the part
 * that finishes a response, and each burst ends with a `data-resume-cursor`
 * the next request resumes from; this follower reconnects for as long as it
 * runs.
 *
 * Chunks are folded by the AI SDK's Chat state, one instance per response
 * segment. Its UI snapshots share nested tool results instead of deep-copying
 * the complete message on every fragment. A segment opens at a response's `start`
 * (which also reopens a response an interaction paused, under the same id) and
 * closes at its `finish`; a connection that drops in between does not close
 * it, so a reply resumes exactly where it was cut. A segment that continues a
 * message already shown — one history loaded while it was still running —
 * starts from that message. History names unfinished text/reasoning parts,
 * whose prefixes are seeded through the SDK so later deltas continue them.
 *
 * A segment's Chat uses a local transport over the already-open stream; it
 * sends no request or user message. Responses can pause and continue, run
 * while the person's next message waits, or start without a user message.
 */

/** What the follower tells the page, and what it reads from it. */
export interface FollowerHost {
  /** The messages as shown now. */
  messages(): readonly UIMessage[];
  /** A message the stream wrote: in place of the one with its id, or after everything. */
  write(message: UIMessage): void;
  /**
   * A response started, or a paused one continued (`reopened`) — with its
   * `start` sent again or without it.
   */
  responseStarted(messageId: string, metadata: ResponseMetadata, reopened: boolean): void;
  /** A response paused at an interaction. */
  responsePaused(messageId: string): void;
  responseEnded(messageId: string): void;
  inputFailed(failure: InputFailure & { input_id: string }): void;
  interactionOpened(interaction: PendingInteraction): void;
  interactionClosed(interactionId: string): void;
  childRunsChanged(): void;
  /**
   * What a page shows of the session record changed, such as the title
   * generated after the first response; the page reads the session again.
   * It arrives between responses as often as inside one.
   */
  sessionChanged(): void;
  /** The stream delivers again. */
  connected(): void;
  /** A request for the stream failed; the follower retries on its own. */
  disconnected(error: unknown): void;
  /**
   * What the stream delivered could not be folded into its message. The
   * follower has stopped; the page reads history again and follows from there.
   */
  broken(error: unknown): void;
}

interface Segment {
  /** The response this segment writes; unknown until its `start` names it. */
  messageId: string | null;
  /** Hand a chunk to the segment's reader; ignored once that reader has stopped. */
  put(chunk: UIMessageChunk): void;
  close(): void;
  /** Text, reasoning and tool-input parts begun inside this segment. */
  begun: Set<string>;
  /** An `error` part waiting for the `finish` that says it was the response's. */
  pendingError: string | null;
  /** Settles once everything fed to this segment has been written. */
  written: Promise<void>;
}

const RETRY_DELAYS_MS = [250, 1_000, 2_000, 5_000] as const;
/** The pause before reading again after a request that delivered nothing. */
const EMPTY_RESPONSE_DELAY_MS = 250;

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

function resumeCursor(chunk: UIMessageChunk): number | null {
  const seq = (chunk as { data?: { seq?: unknown } }).data?.seq;
  return typeof seq === 'number' && Number.isInteger(seq) && seq >= 0 ? seq : null;
}

/**
 * One chunk as a comparable string, its keys in a fixed order: a part read
 * back from the output log (PostgreSQL JSONB) does not keep the key order of
 * the same part delivered live.
 */
function chunkKey(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => (
    item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.entries(item as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
      : item
  ));
}

export class SessionFollower {
  private cursor: number;
  private readonly abort = new AbortController();
  private readonly transport: DefaultChatTransport<UIMessage>;
  private segment: Segment | null = null;
  /** The last segment of each response, to wait on before changing what it wrote. */
  private readonly lastSegment = new Map<string, Promise<void>>();
  /** The segment closed last: a new one starts from what it finished writing. */
  private closing: Promise<void> = Promise.resolve();
  /**
   * The chunks taken since the last cursor. A request after a dropped
   * connection starts at that cursor, so it delivers these again first; they
   * are skipped rather than written twice.
   */
  private sinceCursor: string[] = [];
  private stopped = false;

  constructor(
    private readonly sessionId: string,
    afterSeq: number,
    private readonly host: FollowerHost,
  ) {
    this.cursor = afterSeq;
    this.transport = new DefaultChatTransport<UIMessage>({
      api: buildSessionStreamUrl(sessionId, afterSeq),
      credentials: 'include',
      prepareReconnectToStreamRequest: () => ({
        api: buildSessionStreamUrl(this.sessionId, this.cursor),
        credentials: 'include',
      }),
      // A socket that dies under the browser stays pending forever; ending
      // the body on silence turns it into a request this loop repeats.
      fetch: async (input, init) => {
        const response = await consoleFetch(input, init);
        return response.ok ? withStreamLiveness(response) : response;
      },
    });
  }

  start(): void {
    void this.run();
  }

  stop(): void {
    this.stopped = true;
    this.abort.abort();
    this.closeSegment();
  }

  /** The response a segment is writing now. */
  writing(): string | null {
    return this.segment?.messageId ?? null;
  }

  /** Resolves once nothing the stream already delivered is still being written to `messageId`. */
  async written(messageId: string): Promise<void> {
    await this.lastSegment.get(messageId);
  }

  /**
   * Record a person's approval in the segment writing its response, so the
   * stream's next state of that message keeps it. False when no segment is
   * writing that response, and the page records it on the message itself.
   */
  recordApproval(messageId: string, approvalId: string, approved: boolean, reason?: string): boolean {
    const segment = this.segment;
    if (!segment || segment.messageId !== messageId) return false;
    segment.put({
      type: 'tool-approval-response',
      approvalId,
      approved,
      ...(reason ? { reason } : {}),
    });
    return true;
  }

  private async run(): Promise<void> {
    let failures = 0;
    while (!this.stopped) {
      let delivered = 0;
      try {
        const stream = await this.transport.reconnectToStream({
          chatId: this.sessionId,
          abortSignal: this.abort.signal,
        });
        if (stream === null) {
          throw new Error('SESSION_STREAM_EMPTY: the session stream answered without a body');
        }
        // The server answered: the stream is up, whether or not it has
        // anything to deliver yet. A paused response, a long tool call or an
        // idle session keeps a healthy request open with nothing on it.
        failures = 0;
        this.host.connected();
        const reader = stream.getReader();
        let replay = [...this.sinceCursor];
        for (;;) {
          const { value, done } = await reader.read();
          if (done || this.stopped) break;
          delivered += 1;
          replay = await this.take(value, replay);
        }
        if (this.stopped) return;
      } catch (error) {
        if (this.stopped) return;
        if (TypeValidationError.isInstance(error) || JSONParseError.isInstance(error)) {
          this.fail(error);
          return;
        }
        failures += 1;
        this.host.disconnected(error);
        await delay(RETRY_DELAYS_MS[Math.min(failures, RETRY_DELAYS_MS.length) - 1], this.abort.signal);
        continue;
      }
      // The response that ended this request is done, or something closed a
      // quiet stream: either way the next request starts at the cursor.
      if (delivered === 0) await delay(EMPTY_RESPONSE_DELAY_MS, this.abort.signal);
    }
  }

  private fail(error: unknown): void {
    if (this.stopped) return;
    this.stop();
    this.host.broken(error);
  }

  /** Take one chunk; returns what is still to be skipped as already taken. */
  private async take(chunk: UIMessageChunk, replay: string[]): Promise<string[]> {
    if (chunk.type === 'data-resume-cursor') {
      const cursor = resumeCursor(chunk);
      if (cursor === null) {
        this.fail(new Error(`SESSION_STREAM_CURSOR_INVALID: ${chunkKey(chunk)}`));
        return [];
      }
      this.cursor = cursor;
      this.sinceCursor = [];
      return [];
    }
    if ((chunk as { transient?: unknown }).transient === true) {
      // A transient part is never stored, so a request resuming at the
      // cursor does not deliver it again: it is neither skipped as a replay
      // nor counted as one.
      await this.handle(chunk);
      return replay;
    }
    const key = chunkKey(chunk);
    if (replay.length > 0 && replay[0] === key) return replay.slice(1);
    this.sinceCursor.push(key);
    await this.handle(chunk);
    return [];
  }

  private async handle(chunk: UIMessageChunk): Promise<void> {
    switch (chunk.type) {
      case 'start':
        await this.startResponse(chunk);
        return;
      case 'finish':
        await this.finishResponse(chunk);
        return;
      case 'error': {
        // An `error` part is the response's when its `finish` follows (a
        // response lost with its box). One the route writes when the stream
        // itself fails is not, and the next part says so.
        const segment = this.segment ?? await this.continueOpenResponse(chunk.type);
        if (segment) segment.pendingError = chunk.errorText;
        return;
      }
      case 'data-input-failed': {
        const data = (chunk as { data?: InputFailure & { input_id?: unknown } }).data;
        if (data && typeof data.input_id === 'string' && data.input_id) {
          this.host.inputFailed({ ...data, input_id: data.input_id });
        }
        return;
      }
      case 'data-interaction': {
        const data = (chunk as { data?: Record<string, unknown> }).data;
        await this.feed(chunk);
        if (data && typeof data.interaction_id === 'string') {
          if (data.status && data.status !== 'open') {
            this.host.interactionClosed(data.interaction_id);
            return;
          }
          // The record names the response it pauses on the session's reads;
          // on the stream it arrives inside that response's message.
          this.host.interactionOpened({
            ...data,
            response_id: (data.response_id as string | undefined) ?? this.segment?.messageId ?? undefined,
          } as unknown as PendingInteraction);
        }
        return;
      }
      case 'data-subagent':
        await this.feed(chunk);
        this.host.childRunsChanged();
        return;
      case 'data-session-changed':
        // About the session, not a response: it belongs to no message.
        this.host.sessionChanged();
        return;
      case 'data-transcript-changed':
        this.host.childRunsChanged();
        return;
      default:
        await this.feed(chunk);
    }
  }

  private async startResponse(chunk: Extract<UIMessageChunk, { type: 'start' }>): Promise<void> {
    const messageId = String(chunk.messageId ?? '').trim();
    if (!messageId) {
      await this.feed(chunk);
      return;
    }
    if (this.segment && this.segment.messageId !== messageId) this.closeSegment();
    let segment = this.segment;
    let reopened = segment !== null;
    if (!segment) {
      // A response that reopens continues from everything its last segment wrote.
      await this.closing;
      const shown = this.host.messages().find((message) => message.id === messageId) ?? null;
      reopened = shown !== null;
      segment = this.openSegment(shown);
    }
    segment.messageId = messageId;
    segment.pendingError = null;
    segment.put(chunk);
    segment.put({ type: 'message-metadata', messageMetadata: { status: 'running' } });
    this.host.responseStarted(
      messageId,
      (chunk.messageMetadata ?? {}) as ResponseMetadata,
      reopened,
    );
  }

  private async finishResponse(chunk: Extract<UIMessageChunk, { type: 'finish' }>): Promise<void> {
    const segment = this.segment ?? await this.continueOpenResponse(chunk.type);
    if (!segment) return;
    // `tool-calls` closes a message segment at an interaction; the response
    // reopens with `start` under the same id when it continues.
    const paused = chunk.finishReason === 'tool-calls';
    segment.put({
      type: 'message-metadata',
      messageMetadata: paused
        ? { status: 'waiting' }
        : {
            status: 'ended',
            ...(segment.pendingError ? { error: { message: segment.pendingError } } : {}),
          },
    });
    segment.put(chunk);
    const messageId = segment.messageId;
    this.closeSegment();
    if (!messageId) return;
    if (paused) this.host.responsePaused(messageId);
    else this.host.responseEnded(messageId);
  }

  private async feed(chunk: UIMessageChunk): Promise<void> {
    const segment = this.segment ?? await this.continueOpenResponse(chunk.type);
    if (!segment) return;
    segment.pendingError = null;
    for (const part of this.stitch(segment, chunk)) segment.put(part);
  }

  /**
   * A part with no segment open continues the newest response that has not
   * ended: one history showed still running (the page loaded while it was
   * being written), or one that paused at an interaction and goes on without
   * its `start` sent again — to its next interaction, or to the `error` and
   * `finish` of a response lost while it waited.
   */
  private async continueOpenResponse(chunkType: string): Promise<Segment | null> {
    await this.closing;
    const open = lastOpenResponse(this.host.messages());
    if (!open) {
      console.warn(`[session stream] a ${chunkType} part arrived outside any response`);
      return null;
    }
    const segment = this.openSegment(open);
    segment.messageId = open.id;
    const metadata = responseMetadata(open);
    if (metadata.status === 'waiting') {
      // The same as the `start` a continued response sends when it sends one.
      segment.put({ type: 'message-metadata', messageMetadata: { status: 'running' } });
      this.host.responseStarted(open.id, metadata, true);
    }
    return segment;
  }

  /**
   * The chunks that apply `chunk` to this segment's message.
   *
   * A segment that continues a message history loaded starts after parts
   * that began before its cursor; the AI SDK refuses a delta for a part it
   * did not see begin. History's open prefixes are seeded when the segment
   * opens. A tool's partial input is left to the complete input that follows it.
   */
  private stitch(segment: Segment, chunk: UIMessageChunk): UIMessageChunk[] {
    switch (chunk.type) {
      case 'text-start':
      case 'reasoning-start':
        segment.begun.add(`${chunk.type.split('-')[0]}:${chunk.id}`);
        return [chunk];
      case 'text-delta':
      case 'reasoning-delta': {
        const kind = chunk.type === 'text-delta' ? 'text' : 'reasoning';
        const key = `${kind}:${chunk.id}`;
        if (segment.begun.has(key)) return [chunk];
        segment.begun.add(key);
        return [{ type: `${kind}-start`, id: chunk.id } as UIMessageChunk, chunk];
      }
      case 'text-end':
      case 'reasoning-end': {
        const key = `${chunk.type.split('-')[0]}:${chunk.id}`;
        if (!segment.begun.delete(key)) return [];
        return [chunk];
      }
      case 'tool-input-start':
        segment.begun.add(`tool:${chunk.toolCallId}`);
        return [chunk];
      case 'tool-input-delta':
        return segment.begun.has(`tool:${chunk.toolCallId}`) ? [chunk] : [];
      default:
        return [chunk];
    }
  }

  private openSegment(base: UIMessage | null): Segment {
    const continuation = base ? responseMetadata(base).continuation_parts ?? [] : [];
    const continuedIndexes = new Map(continuation.map((part, index) => [part.index, index]));
    const seed = base ? structuredClone(base) : undefined;
    if (seed) {
      seed.parts = seed.parts.filter((_, index) => !continuedIndexes.has(index));
      seed.metadata = { ...responseMetadata(seed), continuation_parts: [] };
    }
    const retainedCount = seed?.parts.length ?? 0;
    let kept = 0;
    const partOrder = base?.parts.map((_, index) => {
      const continued = continuedIndexes.get(index);
      return continued === undefined ? kept++ : retainedCount + continued;
    }) ?? [];
    let input!: ReadableStreamDefaultController<UIMessageChunk>;
    let open = true;
    let chat: Chat<UIMessage> | undefined;
    const stream = new ReadableStream<UIMessageChunk>({
      start(controller) { input = controller; },
      cancel() { open = false; },
    });
    // The reader cancels its input when it fails; what is handed to it
    // after that has nowhere to go.
    const segment: Segment = {
      messageId: base?.id ?? null,
      put: (chunk) => {
        if (!open) return;
        try { input.enqueue(chunk); } catch { open = false; }
      },
      close: () => {
        if (this.stopped) void chat?.stop();
        if (!open) return;
        open = false;
        try { input.close(); } catch { /* the reader already stopped */ }
      },
      begun: new Set(),
      pendingError: null,
      written: Promise.resolve(),
    };
    segment.written = (async () => {
      chat = new Chat<UIMessage>({
        messages: seed ? [seed] : [],
        transport: {
          sendMessages: async () => stream,
          reconnectToStream: async () => {
            throw new Error('SESSION_SEGMENT_RECONNECT: reconnect belongs to the session follower');
          },
        },
        onError: (error) => this.fail(error),
      });
      let seeded = continuation.length === 0;
      let publishing = true;
      const publish = () => {
        if (!publishing || this.stopped) return;
        const message = chat!.lastMessage;
        if (!message?.id) return;
        if (!seeded) {
          seeded = continuation.every(({ index }, offset) => {
            const part = message.parts[retainedCount + offset];
            const prefix = base!.parts[index];
            return part?.type === prefix.type && 'text' in part && 'text' in prefix && part.text.startsWith(prefix.text);
          });
          if (!seeded) return;
        }
        // Seeded prefixes are appended by the SDK. Publish them in their
        // original positions after all prefixes have been restored.
        const parts = continuation.length === 0 ? message.parts : [
          ...partOrder.map((index, original) => message.parts[index] ?? base!.parts[original]),
          ...message.parts.slice(partOrder.length),
        ];
        this.host.write(retractMessageParts({ ...message, parts }));
      };
      const unsubscribe = chat['~registerMessagesCallback'](publish, 50);
      for (const { index, id } of continuation) {
        const part = base!.parts[index];
        if (!part || (part.type !== 'text' && part.type !== 'reasoning')) {
          throw new Error('SESSION_HISTORY_CONTINUATION_INVALID: expected a text or reasoning prefix');
        }
        segment.begun.add(`${part.type}:${id}`);
        segment.put({ type: `${part.type}-start`, id, providerMetadata: part.providerMetadata });
        segment.put({ type: `${part.type}-delta`, id, delta: part.text });
      }
      try {
        await chat.sendMessage();
        publish();
      } finally {
        publishing = false;
        unsubscribe();
      }
    })().catch((error: unknown) => this.fail(error));
    this.segment = segment;
    return segment;
  }

  private closeSegment(): void {
    const segment = this.segment;
    if (!segment) return;
    this.segment = null;
    segment.close();
    this.closing = segment.written;
    if (segment.messageId) this.lastSegment.set(segment.messageId, segment.written);
  }
}
