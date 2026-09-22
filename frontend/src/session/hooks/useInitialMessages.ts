import { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import type { UIMessage as SDKUIMessage, UIMessagePart } from 'ai';
import { getMessages } from '../../api';
import type { MessageRecord, ContentBlock, PendingInteraction } from '../../types';
import { getPendingToolCallId, shouldBindToolPermissionBlock } from '../pendingToolMatch';
import {
  getPendingInteractionRefreshKey,
  shouldRefetchMessagesForPendingInteractionChange,
} from '../pendingInteractionRefresh';
import { isTransientHistoryLoadError } from '../sessionPageLoading';
import { getToolResultTerminalState } from '../toolResultState';
import { getMessageTurnId, platformTurnMetadata } from '../messageIdentity';
import { toPrettyJson } from '../../utils/format';

/**
 * Convert backend MessageRecord[] to AI SDK UIMessage[] format.
 * Each message's `blocks` are mapped to `parts`:
 * - text -> TextUIPart
 * - thinking -> ReasoningUIPart
 * - tool_use -> DynamicToolUIPart (state: input-available | output)
 * - tool_result -> merged into corresponding tool_use part as output
 * - result -> DataUIPart (data-result)
 */

type AnyPart = UIMessagePart<any, any>;
type DataInteractionPart = AnyPart & {
  type: 'data-interaction';
  data: Record<string, unknown>;
};
function isDataInteractionPart(part: AnyPart): part is DataInteractionPart {
  return (
    typeof part.type === 'string' &&
    part.type === 'data-interaction' &&
    !!(part as { data?: unknown }).data &&
    typeof (part as { data?: unknown }).data === 'object'
  );
}

function buildDataInteractionPart(pi: PendingInteraction): DataInteractionPart {
  return {
    type: 'data-interaction',
    data: pi as unknown as Record<string, unknown>,
  };
}

function appendDataInteractionPart(parts: AnyPart[], pi: PendingInteraction): AnyPart[] {
  const existingIndex = parts.findIndex(isDataInteractionPart);
  const nextPart = buildDataInteractionPart(pi);
  if (existingIndex >= 0) {
    const existing = parts[existingIndex] as DataInteractionPart;
    const existingInteractionId = String(existing.data.interaction_id ?? '');
    if (existingInteractionId === pi.interaction_id) {
      return parts;
    }
    const next = [...parts];
    next[existingIndex] = nextPart;
    return next;
  }
  return [...parts, nextPart];
}

function hasPendingDynamicToolPart(parts: AnyPart[], pi: PendingInteraction): boolean {
  const expectedToolCallId = getPendingToolCallId(pi);
  const expectedToolName = String(pi.tool_name ?? '').trim();
  return parts.some((part) => {
    if (part.type !== 'dynamic-tool') {
      return false;
    }
    const toolPart = part as Record<string, unknown>;
    const toolCallId = String(toolPart.toolCallId ?? '').trim();
    if (expectedToolCallId && toolCallId === expectedToolCallId) {
      return true;
    }
    if (pi.presentation === 'tool_approval') {
      return false;
    }
    const toolName = String(toolPart.toolName ?? '').trim();
    const state = String(toolPart.state ?? '').trim();
    return (
      !!expectedToolName
      && toolName === expectedToolName
      && state === 'input-available'
      && toolPart.providerExecuted !== true
    );
  });
}

export function attachPendingInteractionToExistingTool(
  messages: SDKUIMessage[],
  pi: PendingInteraction | null,
): SDKUIMessage[] {
  if (!pi) {
    return messages;
  }

  const assistantIndex = messages.findIndex(
    (message) => (
      message.role === 'assistant'
      && getMessageTurnId(message) === pi.turn_id
      && hasPendingDynamicToolPart(message.parts as AnyPart[], pi)
    ),
  );
  if (assistantIndex < 0) {
    return messages;
  }

  const current = messages[assistantIndex];
  const currentParts = current.parts as AnyPart[];
  if (!hasPendingDynamicToolPart(currentParts, pi)) {
    return messages;
  }

  const nextParts = appendDataInteractionPart(currentParts, pi);
  if (nextParts === currentParts) {
    return messages;
  }

  const nextMessages = [...messages];
  nextMessages[assistantIndex] = {
    ...current,
    parts: nextParts,
  };
  return nextMessages;
}

export function blocksToSDKParts(
  blocks: ContentBlock[],
  pi?: PendingInteraction | null,
  toolResultBlocks: readonly ContentBlock[] = blocks,
): AnyPart[] {
  const parts: AnyPart[] = [];
  const toolResultMap = new Map<string, ContentBlock>();
  const pendingToolCallId = pi ? getPendingToolCallId(pi) : '';

  // A caller displaying several messages can supply their shared transcript
  // scope; tool pairing never reaches outside those explicitly passed blocks.
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
        } else if (
          pi
          && pi.presentation === 'tool_approval'
          && pendingToolCallId
          && shouldBindToolPermissionBlock(pi, block.id)
        ) {
          parts.push({
            type: 'dynamic-tool',
            toolName: block.name,
            toolCallId: block.id,
            state: 'approval-requested',
            providerExecuted: false,
            input: block.input,
            approval: { id: pi.interaction_id },
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
          },
        } as unknown as AnyPart);
        break;
      case 'turn_failure':
        parts.push({
          type: 'data-turn-failure',
          data: {
            error: block.error,
            failure_phase: block.failure_phase,
          },
        } as unknown as AnyPart);
        break;
      case 'api_retry':
        parts.push({
          type: 'data-api-retry',
          // Same id the live frame carries. The SDK replaces a data part it
          // already holds under this id; without it a turn still streaming
          // when the page reloaded grows a second copy of the same event.
          ...(block.id ? { id: block.id } : {}),
          data: {
            attempt: block.attempt,
            max_retries: block.max_retries,
            error_status: block.error_status,
            error: block.error,
          },
        } as unknown as AnyPart);
        break;
      case 'process_block':
        parts.push({
          type: 'data-process-block',
          // The header's own id: the SDK keys a data part by it, so a page
          // that re-reads the same window replaces the header instead of
          // laying a second copy beside the one already open.
          id: block.process_details.block_id,
          data: block.process_details,
        } as unknown as AnyPart);
        break;
      case 'ui_data':
        parts.push(block.part as AnyPart);
        break;
      // tool_result handled via tool_use merge above
    }
  }

  if (parts.length === 0) {
    return [{ type: 'text', text: '' }];
  }

  return parts;
}

export function convertRecordToSDKMessage(
  msg: MessageRecord,
  pi?: PendingInteraction | null,
): SDKUIMessage {
  const matchingPi = pi && msg.role === 'assistant' && msg.turn_id === pi.turn_id ? pi : null;
  // A user message is its text plus anything text cannot carry. `content` holds
  // the input's text and `blocks` holds the rest — an image pasted into the
  // composer lives there — so building this role from `content` alone would
  // render a caption with no picture.
  const parts: AnyPart[] = msg.role === 'user'
    ? [
        { type: 'text' as const, text: msg.content || '' },
        // Guarded because `blocksToSDKParts` answers an empty list with one
        // empty text part, which is right for a message built from blocks
        // alone and a second empty bubble under every plain user message.
        ...(msg.blocks?.length ? blocksToSDKParts(msg.blocks, null) : []),
      ]
    : blocksToSDKParts(msg.blocks || [], matchingPi);

  return {
    id: msg.message_id,
    // Session-owned messages, such as a startup greeting, have no platform
    // turn. Their message_id remains their identity in both live and history.
    metadata: msg.turn_id === '' ? undefined : platformTurnMetadata(msg.turn_id),
    client_message_id: msg.client_message_id ?? null,
    role: msg.role,
    parts: matchingPi && hasPendingDynamicToolPart(parts, matchingPi)
      ? appendDataInteractionPart(parts, matchingPi)
      : parts,
  } as SDKUIMessage;
}

function hasMatchingPendingInteractionPart(
  messages: SDKUIMessage[],
  pendingInteraction: PendingInteraction | null | undefined,
): boolean {
  if (!pendingInteraction) {
    return true;
  }

  return messages.some((msg) => {
    if (msg.role !== 'assistant' || getMessageTurnId(msg) !== pendingInteraction.turn_id) {
      return false;
    }
    return msg.parts.some((part) => {
      if (!isDataInteractionPart(part)) {
        return false;
      }
      return String(part.data.interaction_id ?? '') === pendingInteraction.interaction_id;
    });
  });
}

async function fetchAllMessageRecords(sessionId: string): Promise<MessageRecord[]> {
  const allRecords: MessageRecord[] = [];
  let before: string | undefined;
  let hasMore = true;

  while (hasMore) {
    const page = await getMessages(sessionId, before, 50);
    allRecords.unshift(...page.messages);
    hasMore = page.has_more;
    if (hasMore && page.messages.length > 0) {
      before = page.messages[0].created_at;
    }
  }

  return allRecords;
}

export async function loadDurableMessageRecords(
  sessionId: string,
): Promise<MessageRecord[]> {
  return fetchAllMessageRecords(sessionId);
}

export function messageRecordsToSdkMessages(
  records: MessageRecord[],
  pendingInteraction: PendingInteraction | null,
): SDKUIMessage[] {
  return attachPendingInteractionToExistingTool(
    records.map((record) => convertRecordToSDKMessage(record, pendingInteraction)),
    pendingInteraction,
  );
}

export async function loadDurableSdkMessages(
  sessionId: string,
  pendingInteraction: PendingInteraction | null,
): Promise<SDKUIMessage[]> {
  const allRecords = await loadDurableMessageRecords(sessionId);
  return messageRecordsToSdkMessages(allRecords, pendingInteraction);
}

export function useInitialMessages(
  sessionId: string,
  pendingInteraction?: PendingInteraction | null,
) {
  const [messages, setMessages] = useState<SDKUIMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadedOnce, setLoadedOnce] = useState(false);

  // Ref so fetchAll always sees the latest pending interaction without holding
  // it in the effect's dependency list.
  const piRef = useRef(pendingInteraction);
  piRef.current = pendingInteraction;
  const recordsRef = useRef<MessageRecord[]>([]);
  const lastPendingInteractionRefreshKeyRef = useRef<string | null>(null);
  const fetchInFlightRef = useRef<Promise<SDKUIMessage[]> | null>(null);

  useEffect(() => {
    setMessages([]);
    setLoading(true);
    setError(null);
    setLoadedOnce(false);
    recordsRef.current = [];
    lastPendingInteractionRefreshKeyRef.current = null;
    fetchInFlightRef.current = null;
  }, [sessionId]);

  const convertRecordsToSDKMessages = useCallback((
    records: MessageRecord[],
    pi: PendingInteraction | null,
  ) => attachPendingInteractionToExistingTool(
    records.map((record) => convertRecordToSDKMessage(record, pi)),
    pi,
  ), []);

  const applyRecordsToState = useCallback((
    records: MessageRecord[],
    pi: PendingInteraction | null,
  ) => {
    const converted = convertRecordsToSDKMessages(records, pi);
    setMessages(converted);
    return converted;
  }, [convertRecordsToSDKMessages]);

  const fetchAll = useCallback(async (): Promise<SDKUIMessage[]> => {
    if (fetchInFlightRef.current) {
      return fetchInFlightRef.current;
    }

    let pendingFetch: Promise<SDKUIMessage[]> | null = null;
    pendingFetch = (async () => {
      setLoading(true);
      setError(null);
      try {
        const allRecords = await loadDurableMessageRecords(sessionId);
        recordsRef.current = allRecords;
        setLoadedOnce(true);
        const pi = piRef.current ?? null;
        return applyRecordsToState(allRecords, pi);
      } catch (err) {
        setError((err as Error).message);
        return [];
      } finally {
        setLoading(false);
        if (pendingFetch && fetchInFlightRef.current === pendingFetch) {
          fetchInFlightRef.current = null;
        }
      }
    })();

    fetchInFlightRef.current = pendingFetch;
    return pendingFetch;
  }, [sessionId, applyRecordsToState]);

  useEffect(() => {
    const loadError = String(error ?? '').trim();
    if (!sessionId || !isTransientHistoryLoadError(loadError)) {
      return;
    }
    let stopped = false;
    let inFlight = false;
    const retry = async () => {
      if (stopped || inFlight) {
        return;
      }
      inFlight = true;
      try {
        await fetchAll();
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => {
      void retry();
    }, 3000);
    void retry();
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [error, fetchAll, sessionId]);

  const pendingInteractionRefreshKey = useMemo(
    () => getPendingInteractionRefreshKey(pendingInteraction),
    [
      pendingInteraction?.interaction_id,
      pendingInteraction?.tool_call_id,
      pendingInteraction?.turn_id,
      pendingInteraction?.presentation,
    ],
  );

  useEffect(() => {
    const previousKey = lastPendingInteractionRefreshKeyRef.current;
    lastPendingInteractionRefreshKeyRef.current = pendingInteractionRefreshKey;

    if (recordsRef.current.length === 0) {
      return;
    }

    if (shouldRefetchMessagesForPendingInteractionChange({
      hasLoadedRecords: recordsRef.current.length > 0,
      previousKey,
      nextKey: pendingInteractionRefreshKey,
    })) {
      void fetchAll();
      return;
    }

    const pi = piRef.current ?? null;
    applyRecordsToState(recordsRef.current, pi);
  }, [
    pendingInteractionRefreshKey,
    applyRecordsToState,
    fetchAll,
  ]);

  useEffect(() => {
    let cancelled = false;
    fetchAll().then((msgs) => {
      if (cancelled) setMessages([]);
    });
    return () => { cancelled = true; };
  }, [fetchAll]);

  const pendingInteractionReady = useMemo(
    () => hasMatchingPendingInteractionPart(messages, pendingInteraction),
    [messages, pendingInteraction?.interaction_id, pendingInteraction?.turn_id, pendingInteraction?.presentation],
  );

  return {
    messages,
    loading,
    error,
    loadedOnce,
    refetch: fetchAll,
    pendingInteractionReady,
  };
}
