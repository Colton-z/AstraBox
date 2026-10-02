import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { UIMessage as SDKUIMessage } from 'ai';
import { getSessionFileChanges, type SessionFileChangeTurn } from '../../api';
import { useKeepCurrent } from '../../hooks/useKeepCurrent';
import { getMessageTurnId, platformTurnMetadata } from '../messageIdentity';

interface ReadState {
  sessionId: string;
  throughSeq: number;
  turns: Map<string, SessionFileChangeTurn>;
  loaded: boolean;
  error: string | null;
}

function empty(sessionId: string): ReadState {
  return { sessionId, throughSeq: 0, turns: new Map(), loaded: false, error: null };
}

/** Keep settled Diff data independent of the transcript's mounted window. */
export function useSessionFileChanges({
  sessionId, enabled, lifecycleState, lastTurnId, messages,
}: {
  sessionId: string;
  enabled: boolean;
  lifecycleState: string;
  lastTurnId: string | null | undefined;
  messages: SDKUIMessage[];
}) {
  const [read, setRead] = useState<ReadState>(() => empty(sessionId));
  const latest = useRef(read);
  const requestId = useRef(0);
  const controller = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    const id = ++requestId.current;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    const previous = latest.current.sessionId === sessionId ? latest.current : empty(sessionId);
    try {
      const page = await getSessionFileChanges(sessionId, previous.throughSeq, request.signal);
      if (id !== requestId.current) return;
      const turns = new Map(previous.turns);
      for (const turn of page.turns) turns.set(turn.turn_id, turn);
      const next = { sessionId, throughSeq: page.through_seq, turns, loaded: true, error: null };
      latest.current = next;
      setRead(next);
    } catch (error) {
      if (id !== requestId.current || request.signal.aborted) return;
      const next = { ...previous, error: error instanceof Error ? error.message : String(error) };
      latest.current = next;
      setRead(next);
    }
  }, [enabled, sessionId]);

  useEffect(() => {
    void refresh();
    return () => {
      requestId.current += 1;
      controller.current?.abort();
    };
  }, [refresh, lifecycleState, lastTurnId]);
  useKeepCurrent(refresh, {
    follow: enabled && (lifecycleState === 'busy' || lifecycleState === 'background'),
  });

  const current = read.sessionId === sessionId ? read : empty(sessionId);
  const projected = useMemo((): SDKUIMessage[] => {
    if (!enabled || read.sessionId !== sessionId || !read.loaded) return [];
    const settled: SDKUIMessage[] = [...read.turns.values()].flatMap((turn) => turn.messages.map((message) => ({
      id: message.message_id,
      role: 'assistant',
      metadata: platformTurnMetadata(turn.turn_id),
      parts: message.parts,
    })));
    // The completed turn owns its whole projection. Mixing its old mounted
    // messages back in would resurrect superseded results or double live IDs.
    return [...settled, ...messages.filter((message) => !read.turns.has(getMessageTurnId(message)))];
  }, [enabled, read, sessionId, messages]);

  return {
    messages: projected,
    loading: enabled && !current.loaded && !current.error,
    error: current.error,
    refresh,
  };
}
