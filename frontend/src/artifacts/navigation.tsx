import { createContext, useContext, type ReactNode } from 'react';
interface ArtifactNavigation {
  open?: (id: string) => void;
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
