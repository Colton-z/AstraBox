import { useMemo } from 'react';

import type { SessionChildRun } from '../../api';

export interface SubagentEntry {
  childRunId: string;
  parentChildRunId?: string;
  engineKind: string;
  depth: number;
  engineEvent: string;
  engineStatus?: string;
  engineReason?: string;
  closed: boolean;
  active: boolean;
  operations: string[];
  toolCallIds: string[];
  description?: string;
  taskType?: string;
  summary?: string;
  usage?: { total_tokens?: number; tool_uses?: number; duration_ms?: number };
  lastToolName?: string;
}

export interface SubagentRegistry {
  agents: SubagentEntry[];
  getAgent: (childRunId: string | null | undefined) => SubagentEntry | undefined;
  liveCount: number;
  totalCount: number;
}

function optionalString(value: string | null | undefined): string | undefined {
  const normalized = String(value ?? '').trim();
  return normalized || undefined;
}

function toEntry(childRun: SessionChildRun): SubagentEntry {
  return {
    childRunId: childRun.child_run_id,
    parentChildRunId: optionalString(childRun.parent_child_run_id),
    engineKind: childRun.engine_kind,
    depth: childRun.depth,
    engineEvent: childRun.engine_event,
    engineStatus: optionalString(childRun.engine_status),
    engineReason: optionalString(childRun.engine_reason),
    closed: childRun.closed,
    active: childRun.active,
    operations: childRun.operations,
    toolCallIds: childRun.tool_call_ids,
    description: optionalString(childRun.description),
    taskType: optionalString(childRun.task_type),
    summary: optionalString(childRun.summary),
    usage: childRun.usage ?? undefined,
    lastToolName: optionalString(childRun.last_tool_name),
  };
}

/** Map the server's canonical Session child-run projection into console rows. */
export function useSubagentRegistry(childRuns: SessionChildRun[]): SubagentRegistry {
  return useMemo(() => {
    const agents = childRuns.map(toEntry);
    const byId = new Map(agents.map((entry) => [entry.childRunId, entry]));
    return {
      agents,
      getAgent: (childRunId) => {
        const key = String(childRunId ?? '').trim();
        return key ? byId.get(key) : undefined;
      },
      liveCount: agents.filter((entry) => entry.active).length,
      totalCount: agents.length,
    };
  }, [childRuns]);
}

/** The adapter's own vocabulary, without a platform status translation. */
export function subagentDisplayStatus(entry: SubagentEntry): string {
  return entry.engineStatus ?? entry.engineReason ?? entry.engineEvent;
}
