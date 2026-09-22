import type { ToolResultBlockData } from '../types';

const TOOL_RESULT_STATES = new Set([
  'output-available',
  'output-error',
  'output-denied',
]);

export function getToolResultTerminalState(
  result: Pick<ToolResultBlockData, 'content' | 'is_error' | 'tool_result_state'>,
): 'output-available' | 'output-error' | 'output-denied' {
  const explicitState = String(result.tool_result_state ?? '').trim();
  if (TOOL_RESULT_STATES.has(explicitState)) {
    return explicitState as 'output-available' | 'output-error' | 'output-denied';
  }
  if (result.is_error === true) {
    return 'output-error';
  }
  return 'output-available';
}
