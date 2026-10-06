import { createContext, useContext, type ReactNode } from 'react';
import type { ArtifactMarker } from './types';
interface ArtifactNavigation {
  open?: (id: string) => void;
  appeared?: (marker: ArtifactMarker, toolCallId: string) => void;
  setComposer?: (text: string) => void;
  sharedTranscript?: boolean;
}
const Context = createContext<ArtifactNavigation>({});
export function ArtifactNavigationProvider({
  children,
  ...value
}: ArtifactNavigation & { children: ReactNode }) {
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useArtifactNavigation() {
  return useContext(Context);
}
