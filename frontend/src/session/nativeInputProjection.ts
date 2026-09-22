import type { UIMessage as SDKUIMessage } from 'ai';
import type { ContentBlock } from '../types';
import { blocksToSDKParts } from './hooks/useInitialMessages';
import { getMessageTurnId, platformTurnMetadata } from './messageIdentity';

export interface NativeInputProjectionBoundary {
  clientMessageId: string;
  inputId: string;
  /** Filled once the platform accepts the input into one execution. */
  platformTurnId: string;
  /** Filled once the SDK consumes the input and opens its response message. */
  responseMessageId: string;
  content: string;
  contentBlocks?: ContentBlock[];
}

type MessageIdentity = {
  role: SDKUIMessage['role'];
  id?: unknown;
  client_message_id?: unknown;
};

function normalized(value: unknown): string {
  return String(value ?? '').trim();
}

export function messageMatchesNativeInput(
  message: MessageIdentity,
  boundary: NativeInputProjectionBoundary,
): boolean {
  if (message.role !== 'user') return false;
  const messageId = normalized(message.id);
  const clientMessageId = normalized(message.client_message_id);
  return (
    messageId === boundary.clientMessageId
    || messageId === `${boundary.clientMessageId}:user`
    || messageId === `${boundary.inputId}:user`
    || clientMessageId === boundary.clientMessageId
    || clientMessageId === boundary.inputId
  );
}

/**
 * Keep a submitted SDK input visible while the assistant stream is volatile.
 * HTTP delivery, SDK consumption, and assistant chunks are independent facts:
 * none of them may make a user's message disappear while the AI SDK replaces
 * its own message array. The durable user row eventually takes ownership.
 */
export function projectNativeInputs(
  messages: SDKUIMessage[],
  boundaries: NativeInputProjectionBoundary[],
): SDKUIMessage[] {
  let projected = messages;
  for (const boundary of boundaries) {
    if (projected.some((message) => messageMatchesNativeInput(message, boundary))) {
      continue;
    }
    const platformTurnId = normalized(boundary.platformTurnId);
    const userMessage = {
      id: `${boundary.inputId}:user`,
      ...(platformTurnId ? { metadata: platformTurnMetadata(platformTurnId) } : {}),
      client_message_id: boundary.clientMessageId,
      role: 'user' as const,
      parts: boundary.contentBlocks
        ? blocksToSDKParts(boundary.contentBlocks)
        : [{ type: 'text' as const, text: boundary.content }],
    } as SDKUIMessage;
    const assistantAnchors = new Set([
      platformTurnId,
      normalized(boundary.responseMessageId),
    ].filter(Boolean));
    const responseIndex = assistantAnchors.size === 0 ? -1 : projected.findIndex((message) => {
      if (message.role !== 'assistant') return false;
      return (
        assistantAnchors.has(normalized(message.id))
        || assistantAnchors.has(getMessageTurnId(message))
      );
    });
    projected = responseIndex < 0
      ? [...projected, userMessage]
      : [
          ...projected.slice(0, responseIndex),
          userMessage,
          ...projected.slice(responseIndex),
        ];
  }
  return projected;
}

/**
 * Transfer ownership only when the exact turn reaches its durable boundary and
 * the adopted message array contains that input. An interim history read is not
 * ownership: the SDK may still replace its live array, and an older turn's
 * delayed completion must never retire a newer input boundary.
 */
export function retireAdoptedNativeInputs(
  boundaries: NativeInputProjectionBoundary[],
  adoptedMessages: SDKUIMessage[],
  completedPlatformTurnId: string | null,
): NativeInputProjectionBoundary[] {
  const completedTurnId = normalized(completedPlatformTurnId);
  if (!completedTurnId) return boundaries;
  return boundaries.filter((boundary) => (
    boundary.platformTurnId !== completedTurnId
    || !adoptedMessages.some((message) => messageMatchesNativeInput(message, boundary))
  ));
}
