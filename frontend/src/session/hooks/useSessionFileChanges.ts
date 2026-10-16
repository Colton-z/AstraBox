import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { UIMessage as SDKUIMessage } from 'ai';
import { getSessionFileChanges, type SessionFileChangeResponse } from '../../api';
import { useKeepCurrent } from '../../hooks/useKeepCurrent';

interface ReadState {
  sessionId: string;
  throughSeq: number;
  responses: Map<string, SessionFileChangeResponse>;
  loaded: boolean;
  error: string | null;
}

function empty(sessionId: string): ReadState {
  return { sessionId, throughSeq: 0, responses: new Map(), loaded: false, error: null };
}

/**
 * The Diff panel's file changes, independent of the transcript's loaded window.
 *
 * Ended responses come from the session's record (`GET …/file-changes`), read
 * incrementally after the last position read; each response read replaces what
 * was held for it. A response still running is shown from the conversation's
 * own messages, which the stream keeps current.
 */
export function useSessionFileChanges({
  sessionId, enabled, working, lastResponseId, messages,
}: {
  sessionId: string;
  enabled: boolean;
  /** A response is running, so its changes keep arriving. */
  working: boolean;
  /** The last response that ended, by the session's detail: one more is there to read. */
  lastResponseId: string | null | undefined;
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
      const responses = new Map(previous.responses);
      for (const response of page.responses) responses.set(response.response_id, response);
      const next = { sessionId, throughSeq: page.through_seq, responses, loaded: true, error: null };
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
  }, [refresh, lastResponseId]);
  useKeepCurrent(refresh, { follow: enabled && working });

  const current = read.sessionId === sessionId ? read : empty(sessionId);
  const projected = useMemo((): SDKUIMessage[] => {
    if (!enabled || read.sessionId !== sessionId || !read.loaded) return [];
    const ended: SDKUIMessage[] = [...read.responses.values()].map((response) => ({
      id: response.response_id,
      role: 'assistant',
      parts: response.parts,
    }));
    // An ended response read from the record owns its changes; its copy among
    // the loaded messages would count them twice.
    return [...ended, ...messages.filter((message) => !read.responses.has(message.id))];
  }, [enabled, read, sessionId, messages]);

  return {
    messages: projected,
    loading: enabled && !current.loaded && !current.error,
    error: current.error,
    refresh,
  };
}
