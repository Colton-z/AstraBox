import { createContext, useContext, type ReactNode } from 'react';
import type { SubagentEntry } from '../hooks/useSubagentRegistry';

// Thin context so deeply-nested tool cards can ask SessionPage to switch
// its right-side panel to the Agents tab and select a specific subagent by
// child-run id, without dragging a setter through every MessageParts +
// ToolPartCard prop in between. `SharePage` renders `MessageBubble` with no
// provider around it, so a read-only shared transcript has no navigation links.
interface SubagentNavigationValue {
  openSubagent: (childRunId: string) => void;
  agents: SubagentEntry[];
}

const SubagentNavigationContext = createContext<SubagentNavigationValue>({
  openSubagent: () => {},
  agents: [],
});

export function SubagentNavigationProvider({
  openSubagent,
  agents,
  children,
}: {
  openSubagent: (childRunId: string) => void;
  agents: SubagentEntry[];
  children: ReactNode;
}) {
  return (
    <SubagentNavigationContext.Provider value={{ openSubagent, agents }}>
      {children}
    </SubagentNavigationContext.Provider>
  );
}

export function useSubagentNavigation(): SubagentNavigationValue {
  return useContext(SubagentNavigationContext);
}
