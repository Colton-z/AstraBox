import { useTranslation } from 'react-i18next';
import { ResizablePanel } from '@/components/ui/resizable';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import TerminalPanel from '../../components/TerminalPanel';
import DiffPanel from '../../components/DiffPanel';
import type { FileChange } from '../../types';
import type { SubagentRegistry } from '../hooks/useSubagentRegistry';
import {
  getRightPanelTabLabel,
  type SessionRightPanelCapabilities,
  type SessionRightPanelTab,
} from '../sessionCapabilities';
import SessionFilesPanel from './SessionFilesPanel';
import { AgentsListPanel } from './AgentsListPanel';

// Right-side tabbed panel for files, terminal, agents, and diffs. It owns its
// ResizablePanel so the parent only assembles it beside the left pane and the
// handle between them.
export function SessionRightPanel({
  rightTab,
  setRightTab,
  rightPanelCaps,
  uniqueChangedFiles,
  subagentRegistry,
  childRunProjectionError,
  sessionId,
  filesPanelEnabled,
  runtimeUnavailableMessage,
  filesRefreshNonce,
  runtimeAccessReady,
  lifecycleState,
  terminalCwd,
  setTerminalCwd,
  selectedChildRunId,
  setSelectedChildRunId,
  fileChanges,
  selectedDiffFile,
  setSelectedDiffFile,
}: {
  rightTab: SessionRightPanelTab;
  setRightTab: (tab: SessionRightPanelTab) => void;
  rightPanelCaps: SessionRightPanelCapabilities;
  uniqueChangedFiles: number;
  subagentRegistry: SubagentRegistry;
  childRunProjectionError: string | null;
  sessionId: string;
  filesPanelEnabled: boolean;
  runtimeUnavailableMessage: string | undefined;
  filesRefreshNonce: number;
  runtimeAccessReady: boolean;
  lifecycleState: string;
  terminalCwd: string | null;
  setTerminalCwd: (cwd: string) => void;
  selectedChildRunId: string | null;
  setSelectedChildRunId: (id: string | null) => void;
  fileChanges: FileChange[];
  selectedDiffFile: string | null;
  setSelectedDiffFile: (path: string) => void;
}) {
  const { t } = useTranslation();
  const enabledTabs = new Set(rightPanelCaps.tabs);
  return (
    <ResizablePanel defaultSize={30} minSize={20} className="flex flex-col overflow-hidden border-l border-border bg-card/30">
      <Tabs value={rightTab} onValueChange={(v) => setRightTab(v as SessionRightPanelTab)} className="flex flex-col h-full">
        {/* The line variant owns both the tab-list hairline and the active
            trigger indicator. Local height or pseudo-element offsets would
            compete with the variant-prefixed utilities in `ui/tabs.tsx`, whose
            generated selectors win the cascade. */}
        <TabsList
          variant="line"
          className="w-full shrink-0 justify-start gap-0 border-b border-border bg-transparent px-2"
        >
          {rightPanelCaps.tabs.map((tab) => (
            <TabsTrigger
              key={tab}
              value={tab}
              // `flex-none`: the kit's trigger is `flex-1`, an equal split of
              // the strip. This panel narrows to a fifth of the window and two
              // of its labels carry a count — "Agents (12)" needs about 90px
              // with the padding, more than a quarter of the strip at the
              // panel's minimum — so an equal split spills a label over its
              // neighbour. Content-sized tabs are also what the strip's
              // `justify-start` describes.
              className="flex-none px-3 text-xs font-medium tracking-[0.01em]"
            >
              {getRightPanelTabLabel(tab, t, {
                changedFileCount: uniqueChangedFiles,
                liveSubagentCount: subagentRegistry.liveCount,
              })}
            </TabsTrigger>
          ))}
        </TabsList>
        {/* Render `TabsContent` only for capabilities that also render a
            trigger. Base UI links each tab and panel through `aria-controls`
            and `aria-labelledby`; either side without the other leaves a
            dangling reference.

            These panels do not set a display utility, so Base UI's mount and
            `hidden` behavior controls visibility. The panels in `App.tsx` set
            `flex` and therefore need an explicit `not-data-hidden:flex`
            guard. */}
        {enabledTabs.has('files') && (
          <TabsContent value="files" className="flex-1 min-h-0 overflow-hidden">
            <SessionFilesPanel
              sessionId={sessionId}
              enabled={filesPanelEnabled}
              disabledMessage={runtimeUnavailableMessage}
              refreshSignal={filesRefreshNonce}
            />
          </TabsContent>
        )}
        {enabledTabs.has('terminal') && (
          <TabsContent value="terminal" className="flex-1 min-h-0 overflow-hidden">
            <TerminalPanel
              sessionId={sessionId}
              enabled={runtimeAccessReady && (lifecycleState === 'ready' || lifecycleState === 'busy' || lifecycleState === 'background')}
              initialCwd={terminalCwd}
              onCwdChange={setTerminalCwd}
            />
          </TabsContent>
        )}
        {enabledTabs.has('agents') && (
          <TabsContent value="agents" className="flex-1 min-h-0 overflow-hidden">
            <AgentsListPanel
              registry={subagentRegistry}
              selectedChildRunId={selectedChildRunId}
              onSelect={setSelectedChildRunId}
              projectionError={childRunProjectionError}
            />
          </TabsContent>
        )}
        {enabledTabs.has('diff') && (
          <TabsContent value="diff" className="flex-1 min-h-0 overflow-hidden">
            <DiffPanel fileChanges={fileChanges} selectedFile={selectedDiffFile} onSelectFile={setSelectedDiffFile} />
          </TabsContent>
        )}
      </Tabs>
    </ResizablePanel>
  );
}
