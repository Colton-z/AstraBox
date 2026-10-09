import { afterEach, describe, expect, it, vi } from 'vitest';

import { ensurePrimaryAssistant, openMainConversation, startAssistantConversation } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('startAssistantConversation', () => {
  it('starts a conversation without exposing Vault selection', async () => {
    const mockFetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 'OK',
      message: 'ok',
      data: { session_id: 'session-1' },
    }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }));
    vi.stubGlobal('fetch', mockFetch);

    await startAssistantConversation('assistant-1');

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/assistants/assistant-1/conversations');
    expect(init.method).toBe('POST');
    expect(init.body).toBeUndefined();
  });
});

function answer(data: unknown): Response {
  return new Response(JSON.stringify({ code: 'OK', message: 'ok', data }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

describe('the primary Assistant and its main conversation', () => {
  it('reports a null record instead of reading it as no Assistant', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(answer(null)));
    await expect(ensurePrimaryAssistant({ display_name: 'Assistant', timezone: 'UTC' })).rejects.toThrow('/assistants/primary');
    await expect(openMainConversation('assistant-1')).rejects.toThrow('/assistants/assistant-1/main-conversation');
  });

  it('answers the records the server sends', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(answer({ assistant_id: 'asst-1', primary: true }))
      .mockResolvedValueOnce(answer({ session_id: 'session-1' })));
    expect((await ensurePrimaryAssistant({ display_name: 'Assistant', timezone: null })).assistant_id).toBe('asst-1');
    expect((await openMainConversation('asst-1')).session_id).toBe('session-1');
  });
});
