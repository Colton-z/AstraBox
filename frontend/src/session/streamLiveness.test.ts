import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { withStreamLiveness } from './streamLiveness';

function silentAfter(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      // and then nothing, ever: the socket is dead underneath the browser
    },
  });
}

async function readAll(response: Response): Promise<string[]> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  const out: string[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return out;
    out.push(decoder.decode(value));
  }
}

describe('withStreamLiveness', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('ends a stream that carries nothing for longer than the silence limit', async () => {
    const response = new Response(silentAfter([': keepalive\n\n']));
    const guarded = withStreamLiveness(response, { limitMs: 45_000 });

    const reading = readAll(guarded);
    await vi.advanceTimersByTimeAsync(10_000);
    // still inside the limit: nothing has ended
    await vi.advanceTimersByTimeAsync(40_000);

    await expect(reading).resolves.toEqual([': keepalive\n\n']);
  });

  it('keeps a stream that the server keeps alive', async () => {
    const encoder = new TextEncoder();
    let push: ((s: string) => void) | null = null;
    const body = new ReadableStream<Uint8Array>({
      start(controller) { push = (s) => controller.enqueue(encoder.encode(s)); },
    });
    const guarded = withStreamLiveness(new Response(body), { limitMs: 45_000 });
    const reader = guarded.body!.getReader();
    const decoder = new TextDecoder();

    for (let i = 0; i < 6; i += 1) {
      await vi.advanceTimersByTimeAsync(15_000);
      push!(': keepalive\n\n');
      const { done, value } = await reader.read();
      expect(done).toBe(false);
      expect(decoder.decode(value)).toBe(': keepalive\n\n');
    }
    // ninety seconds with a keepalive every fifteen: never cut
  });

  it('passes a normal end through', async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(encoder.encode('data: x\n\n')); controller.close(); },
    });
    await expect(readAll(withStreamLiveness(new Response(body)))).resolves.toEqual(['data: x\n\n']);
  });
});
