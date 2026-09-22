import { afterEach, describe, expect, it, vi } from 'vitest';

import { startAssistantConversation } from './api';

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
