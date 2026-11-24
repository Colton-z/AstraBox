import { createContext, useCallback, useContext, useRef, useState } from 'react';
import { matchPath, useLocation } from 'react-router-dom';

import { AssistantPanelView } from './AssistantPanelView';
import type { AssistantRecord } from './types';

/**
 * How many Assistant pages stay loaded while the console shows another page.
 * Each one keeps its runtime's web application running in a hidden frame.
 */
const KEPT_PANELS = 3;

const ShowPanel = createContext<((record: AssistantRecord) => void) | null>(null);

/** Show this Assistant's own page in the console's content area. */
export function useShowAssistantPanel(): (record: AssistantRecord) => void {
  const show = useContext(ShowPanel);
  if (!show) throw new Error('useShowAssistantPanel needs an AssistantPanelHost above it');
  return show;
}

/**
 * Keeps the Assistant pages the user opened loaded while they use the rest of
 * the console.
 *
 * An Assistant's page is its runtime's own web application in a frame, signed
 * in through a single-use address on a hostname of its own. A frame opened
 * again needs a new address and reloads the whole application, its scripts
 * included, since a new hostname shares nothing with the browser's cache. The
 * host therefore renders those pages beside the routes and hides every one but
 * the current route's, so returning to one shows it as it was left. The `KEPT_PANELS` most recently shown stay; the
 * least recently shown one is unloaded and opens afresh next time.
 */
export function AssistantPanelHost({ children }: { children: React.ReactNode }) {
  // In the order first shown, which is the order rendered: moving a frame's
  // element in the document reloads it, so a return never reorders them.
  const [panels, setPanels] = useState<{ record: AssistantRecord; shown: number }[]>([]);
  const shows = useRef(0);
  const { pathname } = useLocation();
  const activeId = matchPath('/assistants/:assistantId', pathname)?.params.assistantId ?? '';

  const show = useCallback((record: AssistantRecord) => {
    const shown = ++shows.current;
    setPanels((current) => {
      if (current.some((panel) => panel.record.assistant_id === record.assistant_id)) {
        return current.map((panel) => (
          panel.record.assistant_id === record.assistant_id ? { ...panel, shown } : panel
        ));
      }
      const next = [...current, { record, shown }];
      if (next.length <= KEPT_PANELS) return next;
      const oldest = Math.min(...next.map((panel) => panel.shown));
      return next.filter((panel) => panel.shown !== oldest);
    });
  }, []);

  return (
    <ShowPanel.Provider value={show}>
      {children}
      {panels.map(({ record }) => {
        const active = record.assistant_id === activeId;
        return (
          <div key={record.assistant_id} hidden={!active} className={active ? 'flex min-h-0 flex-col' : undefined}>
            <AssistantPanelView initial={record} active={active} />
          </div>
        );
      })}
    </ShowPanel.Provider>
  );
}
