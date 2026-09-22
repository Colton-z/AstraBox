import { useCallback, useEffect, useRef, useState } from 'react';
import type { useNavigate } from 'react-router-dom';
import type { SessionRecord } from '../../types';
import { isAssistantConversationSession } from '../../utils/format';

// Owns the terminate and end-conversation actions plus the
// navigate-away-on-delete effect.
export function useSessionTermination({
  session,
  lifecycleState,
  terminate,
  endConversation,
  onSessionChanged,
  navigate,
}: {
  session: SessionRecord;
  lifecycleState: string;
  terminate: () => Promise<void>;
  endConversation: () => Promise<void>;
  onSessionChanged: () => Promise<void>;
  navigate: ReturnType<typeof useNavigate>;
}) {
  const isAgentChat = session.session_kind === 'agent_chat' || !!session.agent_id;
  const isAssistantConversation = isAssistantConversationSession(session);
  const [terminateLoading, setTerminateLoading] = useState(false);
  const handleTerminate = useCallback(async () => {
    setTerminateLoading(true);
    try { await terminate(); void onSessionChanged(); } finally { setTerminateLoading(false); }
  }, [terminate, onSessionChanged]);
  const handleEndConversation = useCallback(async () => {
    setTerminateLoading(true);
    try { await endConversation(); void onSessionChanged(); } finally { setTerminateLoading(false); }
  }, [endConversation, onSessionChanged]);

  const prevLifecycleStateRef = useRef(lifecycleState);
  useEffect(() => {
    const wasDeleted = prevLifecycleStateRef.current === 'deleted';
    prevLifecycleStateRef.current = lifecycleState;
    if (lifecycleState === 'deleted' && !wasDeleted) { void onSessionChanged(); navigate('/'); }
  }, [lifecycleState, navigate, onSessionChanged]);

  return { isAgentChat, isAssistantConversation, terminateLoading, handleTerminate, handleEndConversation };
}
