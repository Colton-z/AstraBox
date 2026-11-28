import { useMemo } from 'react';

import type { SessionRecord } from '../../types';
import type { SubagentEntry, SubagentRegistry } from './useSubagentRegistry';

export type BackgroundTask = NonNullable<SessionRecord['background_tasks']>[number];

/** What a Session's Agent program is running apart from its reply, and what it ran. */
export interface SessionTasks {
  /** Background work that is not a listed child run, such as a shell. */
  commands: BackgroundTask[];
  /** Child runs the engine reports active or still running in the background, in tree order. */
  runningAgents: SubagentEntry[];
  /** The other child runs, in tree order. */
  finishedAgents: SubagentEntry[];
  /** Everything running: each command and each active child run, once. */
  runningCount: number;
}

/**
 * One list of a Session's work, the way Claude Code's `/tasks` lists running
 * shells and subagents together. A background task the engine declares to be
 * a child run is that child run, so it is counted once; until the child-run
 * listing carries it, it stays a command so it is not lost.
 */
export function useSessionTasks(
  backgroundTasks: BackgroundTask[],
  registry: SubagentRegistry,
): SessionTasks {
  return useMemo(() => sessionTasksOf(backgroundTasks, registry), [backgroundTasks, registry]);
}

export function sessionTasksOf(backgroundTasks: BackgroundTask[], registry: SubagentRegistry): SessionTasks {
  const listed = new Set(registry.agents.map((entry) => entry.childRunId));
  const working = new Set(backgroundTasks.map((task) => task.child_run_id).filter(Boolean));
  const commands = backgroundTasks.filter(
    (task) => !task.child_run_id || !listed.has(task.child_run_id),
  );
  const running = (entry: SubagentEntry) => entry.active || working.has(entry.childRunId);
  const runningAgents = registry.agents.filter(running);
  const finishedAgents = registry.agents.filter((entry) => !running(entry));
  return {
    commands,
    runningAgents,
    finishedAgents,
    runningCount: commands.length + runningAgents.length,
  };
}
