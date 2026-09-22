import { createContext, useContext, useEffect, useState } from 'react';

/**
 * What the trail's last segment calls the record you are looking at.
 *
 * The shell derives the trail from the URL, which is right for every level
 * above the record: those segments are their names. The record segment is not.
 * `/manage/environments/claude-code` ends in something a reader recognises;
 * `/manage/agents/229ba529-e307-427a-a502-7bb7098a6bd2` ends in a value that
 * tells them nothing about where they are — and a trail that does not orient
 * the reader is not doing the one job it has (docs/frontend-design.md §1).
 *
 * The shell cannot fix that on its own: only the page has the record, and it
 * has it one fetch later than the trail renders. So the page hands up what to
 * call this segment from its first render — its record's name once that has
 * arrived, and until then the record's type, which the page knows immediately
 * and which is the same word its heading is showing.
 *
 * Never blank, which would make the trail jump a level as the fetch lands, and
 * never the URL segment: that is the opaque key §1 exists to keep out of the
 * trail. The shell still falls back to the segment, so a page that reports
 * nothing keeps the UUID, and a page that reports shows it for the frame
 * between the record rendering and this effect running.
 */
const RecordCrumbContext = createContext<(label: string | null) => void>(() => {});

export function RecordCrumbProvider({
  children,
}: {
  children: (label: string | null) => React.ReactNode;
}) {
  const [label, setLabel] = useState<string | null>(null);
  return (
    <RecordCrumbContext.Provider value={setLabel}>{children(label)}</RecordCrumbContext.Provider>
  );
}

/**
 * Name the current record in the trail. Clears on unmount, so navigating away
 * cannot leave the previous record's name over the next one's page.
 */
export function useRecordCrumb(label: string | null | undefined) {
  const set = useContext(RecordCrumbContext);
  useEffect(() => {
    set(label || null);
    return () => set(null);
  }, [set, label]);
}
