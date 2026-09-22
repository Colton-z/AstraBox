// Returns an i18n key (chat namespace) for the tool's display state, OR the raw
// state string as a passthrough fallback for unknown states. Callers resolve the
// key with t(); a key always starts with 'chat:' so a passthrough state (which
// never does) is rendered verbatim.
export function getToolStateLabelKey({
  state,
  isPending,
  approvalApproved,
}: {
  state: string;
  providerExecuted: boolean;
  isPending: boolean;
  isActiveTurn: boolean;
  approvalApproved?: boolean | null;
}): string {
  if (state === 'input-streaming') return isPending ? 'chat:tool_state.awaiting_confirmation' : 'chat:tool_state.processing';
  if (state === 'input-available') {
    if (isPending) {
      return 'chat:tool_state.awaiting_confirmation';
    }
    return 'chat:tool_state.processing';
  }
  if (state === 'approval-requested') {
    return 'chat:tool_state.awaiting_confirmation';
  }
  if (state === 'approval-responded') {
    if (approvalApproved === false) {
      return 'chat:tool_state.denied';
    }
    return 'chat:tool_state.processing';
  }
  if (state === 'output-available') {
    if (approvalApproved === false) {
      return 'chat:tool_state.denied';
    }
    return 'chat:tool_state.completed';
  }
  if (state === 'output-error') return 'chat:tool_state.failed';
  if (state === 'output-denied') return 'chat:tool_state.denied';
  return String(state);
}

type ToolPartLike = {
  type?: string;
  toolName?: string;
  toolCallId?: string;
  state?: string;
  providerExecuted?: boolean;
};

function isDynamicToolPart(part: unknown): part is ToolPartLike {
  if (!part || typeof part !== 'object') {
    return false;
  }
  return (part as ToolPartLike).type === 'dynamic-tool';
}

const SINGLETON_INTERACTION_TOOL_NAMES = new Set([
  'AskUserQuestion',
  'ExitPlanMode',
]);

function isSupersedableSingletonInteractionPart(part: unknown): part is ToolPartLike {
  if (!isDynamicToolPart(part)) {
    return false;
  }
  if (!SINGLETON_INTERACTION_TOOL_NAMES.has(String(part.toolName ?? ''))) {
    return false;
  }
  const state = String(part.state ?? '');
  if (!['input-streaming', 'input-available', 'approval-requested'].includes(state)) {
    return false;
  }
  return part.providerExecuted !== true;
}

export function filterSupersededSingletonInteractionParts<T>(
  parts: T[],
  pendingToolCallId?: string | null,
): T[] {
  const normalizedPendingToolCallId = String(pendingToolCallId ?? '').trim();
  const lastIndexByToolCallId = new Map<string, number>();
  const lastIndexByTool = new Map<string, number>();
  parts.forEach((part, index) => {
    if (!isDynamicToolPart(part)) {
      return;
    }
    const toolCallId = String(part.toolCallId ?? '').trim();
    if (toolCallId) {
      lastIndexByToolCallId.set(toolCallId, index);
    }
    const toolName = String(part.toolName ?? '');
    if (!SINGLETON_INTERACTION_TOOL_NAMES.has(toolName)) {
      return;
    }
    lastIndexByTool.set(toolName, index);
  });

  return parts.filter((part, index) => {
    if (isDynamicToolPart(part)) {
      const toolCallId = String(part.toolCallId ?? '').trim();
      if (toolCallId && lastIndexByToolCallId.get(toolCallId) !== index) {
        return false;
      }
    }
    if (
      normalizedPendingToolCallId
      && isDynamicToolPart(part)
    ) {
      if (
        SINGLETON_INTERACTION_TOOL_NAMES.has(String(part.toolName ?? ''))
        && String(part.toolCallId ?? '').trim() === normalizedPendingToolCallId
      ) {
        return false;
      }
    }
    if (!isSupersedableSingletonInteractionPart(part)) {
      return true;
    }
    const toolName = String((part as ToolPartLike).toolName ?? '');
    return lastIndexByTool.get(toolName) === index;
  });
}
