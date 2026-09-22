type PollingSessionLike = {
  state?: string | null;
  sandbox_id?: string | null;
  current_turn_id?: string | null;
  pending_interaction?: unknown | null;
  session_kind?: string | null;
  source_type?: string | null;
  agent_id?: string | null;
  agent_runtime?: {
    state?: string | null;
    sandbox_id?: string | null;
    runtime_unavailable?: boolean | null;
  } | null;
};

type PollingOverlayLike = {
  turn_id?: string | null;
} | null | undefined;

function normalizeState(state: string | null | undefined): string {
  return String(state ?? '').trim().toUpperCase();
}

function hasActiveTurn(session: PollingSessionLike | null | undefined): boolean {
  return !!String(session?.current_turn_id ?? '').trim();
}

function hasPendingInteraction(session: PollingSessionLike | null | undefined): boolean {
  return !!session?.pending_interaction;
}

function isAgentChatSession(session: PollingSessionLike | null | undefined): boolean {
  if (!session) {
    return false;
  }
  return session.session_kind === 'agent_chat' || session.source_type === 'agent' || !!String(session.agent_id ?? '').trim();
}

function hasReadyAgentRuntime(session: PollingSessionLike | null | undefined): boolean {
  if (!isAgentChatSession(session)) {
    return true;
  }
  const runtime = session?.agent_runtime;
  if (!runtime || runtime.runtime_unavailable) {
    return false;
  }
  const state = normalizeState(runtime.state);
  if (state === 'DELETED') {
    return true;
  }
  // The box a conversation runs in is recorded on the session under
  // conversation tenancy (the default) and on the Agent under agent tenancy;
  // either is the runtime being attached.
  const boxAttached = !!String(runtime.sandbox_id ?? '').trim()
    || !!String(session?.sandbox_id ?? '').trim();
  return state === 'ACTIVE' && boxAttached;
}

export function isSessionSteadyState(session: PollingSessionLike | null | undefined): boolean {
  const state = normalizeState(session?.state);
  if (!state) {
    return false;
  }
  if (state === 'TERMINATED' || state === 'DELETED') {
    return true;
  }
  if (state === 'BACKGROUND_RUNNING') {
    return false;
  }
  return state === 'READY'
    && !hasActiveTurn(session)
    && !hasPendingInteraction(session)
    && hasReadyAgentRuntime(session);
}

export function shouldPollSessionDetail(
  lifecycleState: string,
  session: PollingSessionLike | null | undefined,
  detailStale = false,
): boolean {
  if (detailStale) {
    return true;
  }
  if (
    lifecycleState === 'creating'
    || lifecycleState === 'recovery'
    || lifecycleState === 'busy'
    || lifecycleState === 'background'
  ) {
    return true;
  }
  if (lifecycleState !== 'ready') {
    return false;
  }
  return !isSessionSteadyState(session);
}

export function shouldPollBackgroundSubagentHistory(options: {
  lifecycleState: string;
  liveSubagentCount: number;
  isSubmitted: boolean;
  isStreaming: boolean;
  hasPendingInteraction: boolean;
}): boolean {
  if (
    options.isSubmitted
    || options.isStreaming
    || options.hasPendingInteraction
  ) {
    return false;
  }
  if (options.lifecycleState === 'background') {
    return true;
  }
  return options.lifecycleState === 'ready' && options.liveSubagentCount > 0;
}

export function shouldPollOverlayTruthGap(
  lifecycleState: string,
  session: PollingSessionLike | null | undefined,
  overlay: PollingOverlayLike,
): boolean {
  if (lifecycleState !== 'ready') {
    return false;
  }
  if (!String(overlay?.turn_id ?? '').trim()) {
    return false;
  }
  return !hasActiveTurn(session) && !hasPendingInteraction(session);
}
