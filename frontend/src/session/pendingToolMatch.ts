import type { UIMessage as SDKUIMessage, UIMessagePart } from 'ai';

import type { PendingInteraction } from '../types';
import { getMessageTurnId } from './messageIdentity';
import { filterSupersededSingletonInteractionParts } from './toolState';

type DynamicToolUIPart = Extract<UIMessagePart<any, any>, { type: 'dynamic-tool' }>;

export interface PendingToolInfo {
  toolCallId: string;
  toolName: string;
  input: unknown;
}

type DataInteractionPart = UIMessagePart<any, any> & {
  type: 'data-interaction';
  data?: Record<string, unknown>;
};

export function getPendingToolCallId(pi: PendingInteraction): string {
  return String(pi.tool_call_id ?? pi.interaction_id ?? '').trim();
}

export function shouldBindToolPermissionBlock(
  pendingInteraction: PendingInteraction | null | undefined,
  toolCallId: string,
): boolean {
  if (!pendingInteraction || pendingInteraction.presentation !== 'tool_approval') {
    return false;
  }
  const expectedToolCallId = getPendingToolCallId(pendingInteraction);
  return !!expectedToolCallId && toolCallId === expectedToolCallId;
}

export function buildPendingToolInfo(
  pendingInteraction: PendingInteraction | null,
  messages: SDKUIMessage[],
): PendingToolInfo | null {
  if (!pendingInteraction) {
    return null;
  }

  const expectedTurnId = String(pendingInteraction.turn_id ?? '').trim();
  const expectedInteractionId = String(pendingInteraction.interaction_id ?? '').trim();
  const expectedToolCallId = getPendingToolCallId(pendingInteraction);
  const expectedToolName = String(pendingInteraction.tool_name ?? '').trim();

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role !== 'assistant' || getMessageTurnId(msg) !== expectedTurnId) continue;
    const visibleParts = filterSupersededSingletonInteractionParts(msg.parts);
    for (const part of visibleParts) {
      if (part.type === 'data-interaction') {
        const data = (part as { data?: Record<string, unknown> }).data;
        if (
          data
          && String(data.interaction_id ?? '').trim()
          && String(data.interaction_id ?? '').trim() !== expectedInteractionId
        ) {
          continue;
        }
      }
      if (part.type !== 'dynamic-tool') {
        continue;
      }
      const dp = part as DynamicToolUIPart;
      const toolCallId = String(dp.toolCallId ?? '').trim();
      if (toolCallId !== expectedToolCallId) {
        continue;
      }
      if (
        dp.state === 'approval-requested'
        || (['input-streaming', 'input-available'].includes(dp.state) && !dp.providerExecuted)
      ) {
        return {
          toolCallId: String(dp.toolCallId ?? ''),
          toolName: String(dp.toolName ?? ''),
          input: dp.input,
        };
      }
    }
  }

  if (pendingInteraction.presentation !== 'tool_approval' && expectedToolName) {
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg.role !== 'assistant' || getMessageTurnId(msg) !== expectedTurnId) continue;
      const visibleParts = filterSupersededSingletonInteractionParts(msg.parts);
      for (const part of visibleParts) {
        if (part.type !== 'dynamic-tool') continue;
        const dp = part as DynamicToolUIPart;
        if (dp.toolName !== expectedToolName) continue;
        if (dp.state === 'input-available' && !dp.providerExecuted) {
          return {
            toolCallId: String(dp.toolCallId ?? ''),
            toolName: String(dp.toolName ?? ''),
            input: dp.input,
          };
        }
      }
    }
  }

  return null;
}

export function getPendingInteractionPart(
  part: unknown,
): PendingInteraction | null {
  if (
    !part
    || typeof part !== 'object'
    || (part as { type?: unknown }).type !== 'data-interaction'
  ) {
    return null;
  }
  const data = (part as DataInteractionPart).data;
  if (!data || typeof data !== 'object') {
    return null;
  }
  const interactionId = String(data.interaction_id ?? '').trim();
  const turnId = String(data.turn_id ?? '').trim();
  const toolName = String(data.tool_name ?? '').trim();
  const presentation = String(data.presentation ?? '').trim();
  if (!interactionId || !turnId || !toolName || !presentation) {
    return null;
  }
  return data as unknown as PendingInteraction;
}

export function getMessagePendingInteraction(
  messages: SDKUIMessage[],
): PendingInteraction | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.role !== 'assistant') {
      continue;
    }
    const visibleParts = filterSupersededSingletonInteractionParts(msg.parts);
    for (let j = visibleParts.length - 1; j >= 0; j -= 1) {
      const pendingInteraction = getPendingInteractionPart(visibleParts[j]);
      if (!pendingInteraction) {
        continue;
      }
      if (buildPendingToolInfo(pendingInteraction, messages)) {
        return pendingInteraction;
      }
    }
  }
  return null;
}
