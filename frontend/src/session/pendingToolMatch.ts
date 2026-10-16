import type { DynamicToolUIPart, UIMessage } from 'ai';

import type { PendingInteraction } from '../types';
import { filterSupersededSingletonInteractionParts } from './toolState';

/**
 * The tool call an interaction waits on, as the transcript shows it.
 *
 * The interaction names its call when the engine tracks one
 * (`tool_call_id`). Otherwise it is the call of the same tool, in the response
 * the interaction pauses, that has its input and no result yet.
 */
export function pendingToolCallId(
  interaction: PendingInteraction | null,
  messages: readonly UIMessage[],
): string {
  if (!interaction) return '';
  const named = String(interaction.tool_call_id ?? '').trim();
  if (named) return named;
  if (interaction.presentation === 'tool_approval') return '';
  const toolName = String(interaction.tool_name ?? '').trim();
  const response = messages.find((message) => (
    message.role === 'assistant' && message.id === interaction.response_id
  ));
  if (!response || !toolName) return '';
  const parts = filterSupersededSingletonInteractionParts(response.parts);
  for (let index = parts.length - 1; index >= 0; index -= 1) {
    const part = parts[index];
    if (part.type !== 'dynamic-tool') continue;
    const tool = part as DynamicToolUIPart;
    if (tool.toolName === toolName && tool.state === 'input-available' && !tool.providerExecuted) {
      return String(tool.toolCallId ?? '');
    }
  }
  return '';
}
