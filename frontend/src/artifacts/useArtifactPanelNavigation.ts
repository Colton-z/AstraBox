import { useCallback, useRef, useState } from 'react';
import type { UIMessage } from 'ai';
import { parseArtifactMarkers } from './marker';
import type { ArtifactMarker } from './types';
export function useArtifactPanelNavigation(
  initialMessages: UIMessage[],
  showPanel: () => void,
) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [seen] = useState(() =>
    new Set(
      initialMessages.flatMap((message) =>
        message.parts.flatMap((part) =>
          part.type === 'dynamic-tool'
            ? parseArtifactMarkers(part.output).map(
                (marker) => `${part.toolCallId}:${marker.id}:${marker.version}`,
              )
            : [],
        ),
      ),
    ),
  );
  const show = useRef(showPanel);
  show.current = showPanel;
  const open = useCallback((id: string) => {
    setSelectedId(id);
    show.current();
  }, []);
  const appeared = useCallback(
    (marker: ArtifactMarker, toolCallId: string) => {
      const key = `${toolCallId}:${marker.id}:${marker.version}`;
      if (seen.has(key)) return;
      seen.add(key);
      setRevision((value) => value + 1);
      open(marker.id);
    },
    [open, seen],
  );
  return { selectedId, setSelectedId, revision, open, appeared };
}
