import type { UIMessagePart } from 'ai';
import type { ContentBlock } from '../types';
import { getToolResultTerminalState } from './toolResultState';
import { toPrettyJson } from '../utils/format';

type AnyPart = UIMessagePart<any, any>;

/**
 * A child run's message content blocks as AI SDK UI message parts.
 *
 * A child run's transcript (`GET …/child-runs/{id}/messages`) carries the
 * engine's own content blocks rather than UI messages. Each block maps to the
 * part the main conversation shows for the same thing, so a sub-agent's tool
 * call reads the same as one the conversation made:
 * text → text, thinking → reasoning, tool_use (with its tool_result) →
 * dynamic-tool, result → data-result, api_retry → data-api-retry, and a
 * ui_data block → the data part it carries.
 *
 * `toolResultBlocks` is the scope tool results are paired from: a caller
 * showing several messages of one child passes all of their blocks, and
 * pairing never reaches outside them.
 */
export function blocksToSDKParts(
  blocks: ContentBlock[],
  toolResultBlocks: readonly ContentBlock[] = blocks,
): AnyPart[] {
  const parts: AnyPart[] = [];
  const toolResultMap = new Map<string, ContentBlock>();
  for (const block of toolResultBlocks) {
    if (block.type === 'tool_result') {
      toolResultMap.set(block.tool_use_id, block);
    }
  }

  for (const block of blocks) {
    switch (block.type) {
      case 'text':
        parts.push({ type: 'text', text: block.text });
        break;
      case 'image':
        // The engine's block keeps the bytes; the reader wants a URL. Both
        // sides of that are base64, so this is the same image either way.
        parts.push({
          type: 'file',
          mediaType: block.source.media_type,
          url: `data:${block.source.media_type};base64,${block.source.data}`,
        } as unknown as AnyPart);
        break;
      case 'thinking':
        parts.push({ type: 'reasoning', text: block.thinking, providerMetadata: {} });
        break;
      case 'tool_use': {
        const result = toolResultMap.get(block.id);
        if (result && result.type === 'tool_result') {
          const state = getToolResultTerminalState(result);
          parts.push({
            type: 'dynamic-tool',
            toolName: block.name,
            toolCallId: block.id,
            state,
            providerExecuted: true,
            input: block.input,
            ...(state === 'output-error'
              ? { errorText: typeof result.content === 'string' ? result.content : toPrettyJson(result.content) }
              : state === 'output-denied'
                ? {}
                : { output: result.content }),
          } as unknown as AnyPart);
        } else {
          parts.push({
            type: 'dynamic-tool',
            toolName: block.name,
            toolCallId: block.id,
            state: 'input-available',
            providerExecuted: false,
            input: block.input,
          } as unknown as AnyPart);
        }
        break;
      }
      case 'result':
        parts.push({
          type: 'data-result',
          data: {
            result: block.result,
            duration_ms: block.duration_ms,
            total_cost_usd: block.total_cost_usd,
            num_turns: block.num_turns,
            usage: block.usage,
            stop_reason: block.stop_reason,
            is_error: block.is_error,
          },
        } as unknown as AnyPart);
        break;
      case 'api_retry':
        parts.push({
          type: 'data-api-retry',
          ...(block.id ? { id: block.id } : {}),
          data: {
            attempt: block.attempt,
            max_retries: block.max_retries,
            error_status: block.error_status,
            error: block.error,
          },
        } as unknown as AnyPart);
        break;
      case 'ui_data':
        parts.push(block.part as AnyPart);
        break;
      // tool_result is merged into its tool_use above.
    }
  }

  if (parts.length === 0) {
    return [{ type: 'text', text: '' }];
  }

  return parts;
}
