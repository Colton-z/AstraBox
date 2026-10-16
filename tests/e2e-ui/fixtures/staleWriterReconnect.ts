/** Pure contract helpers for stale-writer reconnect E2E evidence. */

const PER_SESSION_RUNNER_PORT = 8000;
const SHARED_RUNNER_PORT_BASE = 9000;
const SHARED_RUNNER_PORT_SPAN = 500;

export interface StaleWriterReconnectEvidence {
  expectedSandboxId: string;
  actualSandboxId: string;
  /** The response answering the warm-up message. */
  firstResponseId: string;
  /** The response answering the message sent after the disconnect. */
  secondResponseId: string;
  secondMarker: string;
  secondReplyText: string;
  /** The session detail's state once the second response settled. */
  state: string;
  /** How many responses the session detail still reports running or paused. */
  openResponseCount: number;
  /** The session detail's last ended response, and how it ended. */
  lastResponseId: string;
  lastResponseOutcome: string;
  assistantDelta: number;
  matchingAssistantCount: number;
  /** The type of the last part the platform recorded for the second response. */
  terminalType: string;
}

export interface StaleWriterReconnectVerdict {
  addressedSandboxSurvived: boolean;
  openedDistinctResponse: boolean;
  replyReachedPlatform: boolean;
  completedExactlyOnce: boolean;
  settledReady: boolean;
  durableFinish: boolean;
}

/** Resolve the runner that serves this exact conversation placement. */
export function runnerPortFor(runtimeIdentity: Record<string, unknown>): number {
  const isolatedSessionId = String(runtimeIdentity.isolated_session_id || '').trim();
  if (!isolatedSessionId) return PER_SESSION_RUNNER_PORT;

  const uid = Number(runtimeIdentity.uid);
  if (!Number.isSafeInteger(uid) || uid <= 0) {
    throw new Error(
      `shared-sandbox runtime identity must expose a positive uid; ` +
        `identity=${JSON.stringify(runtimeIdentity)}`,
    );
  }
  return SHARED_RUNNER_PORT_BASE + (uid % SHARED_RUNNER_PORT_SPAN);
}

/** Reduce the independent post-disconnect oracles to the intended contract. */
export function staleWriterReconnectVerdict(
  evidence: StaleWriterReconnectEvidence,
): StaleWriterReconnectVerdict {
  return {
    addressedSandboxSurvived:
      evidence.expectedSandboxId.length > 0 &&
      evidence.actualSandboxId === evidence.expectedSandboxId,
    openedDistinctResponse:
      evidence.firstResponseId.length > 0 &&
      evidence.secondResponseId.length > 0 &&
      evidence.secondResponseId !== evidence.firstResponseId,
    replyReachedPlatform:
      evidence.secondMarker.length > 0 &&
      evidence.secondReplyText.includes(evidence.secondMarker),
    completedExactlyOnce:
      evidence.assistantDelta === 1 && evidence.matchingAssistantCount === 1,
    settledReady:
      evidence.state === 'READY' &&
      evidence.openResponseCount === 0 &&
      evidence.lastResponseId === evidence.secondResponseId &&
      evidence.lastResponseOutcome === 'completed',
    durableFinish: evidence.terminalType === 'finish',
  };
}
