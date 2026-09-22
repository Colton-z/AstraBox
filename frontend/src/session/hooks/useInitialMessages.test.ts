import { describe, expect, it } from 'vitest';

import type { ContentBlock } from '../../types';
import { blocksToSDKParts, convertRecordToSDKMessage } from './useInitialMessages';

describe('tool results scoped to a transcript', () => {
  it('updates a call from a later message without borrowing another transcript result', () => {
    const call: ContentBlock = {
      type: 'tool_use', id: 'native-call', name: 'vendor/tool', input: { command: 'native input' },
    };
    const result: ContentBlock = {
      type: 'tool_result', tool_use_id: 'native-call', content: 'native output', is_error: false,
    };
    const sibling: ContentBlock = { ...result, content: 'sibling output', is_error: true };

    expect(blocksToSDKParts([call], null, [call, result])).toEqual([expect.objectContaining({
      type: 'dynamic-tool', toolCallId: 'native-call', toolName: 'vendor/tool',
      state: 'output-available', input: call.input, output: 'native output',
    })]);
    expect(blocksToSDKParts([call], null, [call, sibling])).toEqual([expect.objectContaining({
      state: 'output-error', errorText: 'sibling output',
    })]);
    expect(blocksToSDKParts([call])).toEqual([expect.objectContaining({
      state: 'input-available', input: call.input,
    })]);
    expect(blocksToSDKParts([call])[0]).not.toHaveProperty('output');
  });
});

describe('blocksToSDKParts API retry projection', () => {
  it('restores the typed retry part without forwarding private or vendor fields', () => {
    const block = {
      type: 'api_retry',
      id: 'retry-3',
      attempt: 3,
      max_retries: 10,
      error_status: 401,
      error: 'authentication_failed',
      retry_delay_ms: 2125,
      raw: { secret: 'must-not-forward' },
    } as unknown as ContentBlock;

    expect(blocksToSDKParts([block])).toEqual([
      {
        type: 'data-api-retry',
        id: 'retry-3',
        data: {
          attempt: 3,
          max_retries: 10,
          error_status: 401,
          error: 'authentication_failed',
        },
      },
    ]);
  });

  it('restores an adapter-declared data part without knowing its fields', () => {
    const part = {
      type: 'data-engine-metrics',
      id: 'metrics-1',
      data: { tokens: 2, newVendorMetric: 9 },
      providerMetadata: { cacheReadTokens: 14 },
    } as const;
    const block: ContentBlock = {
      type: 'ui_data',
      part,
    };

    expect(blocksToSDKParts([block])).toEqual([part]);
  });
});


describe('a durable user message keeps what its text cannot carry', () => {
  it('rebuilds a pasted image beside the caption', () => {
    // The caption is in `content`, while the image is in `blocks`. Reading
    // only the caption would discard the attachment when rebuilding history.
    const message = convertRecordToSDKMessage({
      message_id: 'm1',
      turn_id: 't1',
      role: 'user',
      content: 'look at this',
      blocks: [
        {
          type: 'image',
          source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' },
        },
      ],
    } as never);

    expect(message.parts).toEqual([
      { type: 'text', text: 'look at this' },
      {
        type: 'file',
        mediaType: 'image/png',
        url: 'data:image/png;base64,iVBORw0KGgo=',
      },
    ]);
  });

  it('still renders a plain user message as one text part', () => {
    const message = convertRecordToSDKMessage({
      message_id: 'm2',
      turn_id: 't2',
      role: 'user',
      content: 'no attachments here',
      blocks: [],
    } as never);

    expect(message.parts).toEqual([{ type: 'text', text: 'no attachments here' }]);
  });
});
