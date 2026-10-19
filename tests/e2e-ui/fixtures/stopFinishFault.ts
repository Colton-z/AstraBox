/** Drop a selected response's browser finish while keeping its real control and output. */
import type { Page } from '@playwright/test';

interface Fault {
  target: string | null;
  dropped: string[];
  starts: string[];
  acknowledgements: Array<string | null>;
  held: boolean;
  holdInterrupt: boolean;
  sameIdReplays: number;
  replayStart: () => void;
  releaseInterrupt: () => void;
}
type FaultWindow = Window & { __stopFinishFault: Fault };

export async function installStopFinishFault(page: Page, sessionId: string) {
  await page.addInitScript((sid) => {
    const state: Fault = { target: null, dropped: [], starts: [], acknowledgements: [], held: false,
      holdInterrupt: false, sameIdReplays: 0,
      replayStart: () => { throw new Error('No native start is available for replay'); },
      releaseInterrupt: () => { throw new Error('No interrupt request is held'); } };
    (window as unknown as FaultWindow).__stopFinishFault = state;
    const nativeFetch = window.fetch.bind(window);
    let responseId: string | null = null;
    let nativeStart = '';
    window.fetch = async (...args: Parameters<typeof fetch>) => {
      const input = args[0];
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      const method = args[1]?.method ?? (input instanceof Request ? input.method : 'GET');
      if (url.pathname.endsWith(`/sessions/${sid}/interrupt`) && method === 'POST' && state.holdInterrupt) {
        state.held = true;
        await new Promise<void>((resolve) => { state.releaseInterrupt = resolve; });
        state.held = false;
        state.holdInterrupt = false;
      }
      const response = await nativeFetch(...args);
      if (url.pathname.endsWith(`/sessions/${sid}/interrupt`) && method === 'POST') {
        state.acknowledgements.push(responseId);
      }
      if (!url.pathname.endsWith(`/sessions/${sid}/stream`) || !response.body) return response;
      const decoder = new TextDecoder();
      const encoder = new TextEncoder();
      let buffer = '';
      const body = response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
        start(controller) {
          state.replayStart = () => {
            if (!nativeStart) throw new Error('No native start is available for replay');
            controller.enqueue(encoder.encode(nativeStart));
            state.sameIdReplays += 1;
          };
        },
        transform(chunk, controller) {
          buffer += decoder.decode(chunk, { stream: true });
          for (;;) {
            const boundary = /\r?\n\r?\n/.exec(buffer);
            if (!boundary || boundary.index === undefined) break;
            const frame = buffer.slice(0, boundary.index + boundary[0].length);
            buffer = buffer.slice(frame.length);
            const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:'))
              .map((line) => line.slice(5).trim()).join('\n');
            const part = data && data !== '[DONE]' ? JSON.parse(data) : null;
            if (part?.type === 'start') {
              responseId = part.messageId;
              nativeStart = frame;
              state.starts.push(part.messageId);
            }
            if (part?.type === 'finish' && part.finishReason !== 'tool-calls'
              && state.target !== null && state.target === responseId) {
              state.dropped.push(responseId!);
              state.target = null;
            } else controller.enqueue(encoder.encode(frame));
          }
        },
        flush(controller) {
          buffer += decoder.decode();
          if (buffer) controller.enqueue(encoder.encode(buffer));
        },
      }));
      return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
    };
  }, sessionId);
  return {
    arm: (responseId: string, holdInterrupt = false) => page.evaluate(({ id, hold }) => {
      const state = (window as unknown as FaultWindow).__stopFinishFault;
      state.target = id;
      state.holdInterrupt = hold;
    }, { id: responseId, hold: holdInterrupt }),
    replayStart: () => page.evaluate(() => (window as unknown as FaultWindow).__stopFinishFault.replayStart()),
    release: () => page.evaluate(() => (window as unknown as FaultWindow).__stopFinishFault.releaseInterrupt()),
    read: () => page.evaluate(() => {
      const { target, dropped, starts, held, sameIdReplays, acknowledgements } =
        (window as unknown as FaultWindow).__stopFinishFault;
      return { target, dropped, starts, held, sameIdReplays, acknowledgements };
    }),
  };
}
