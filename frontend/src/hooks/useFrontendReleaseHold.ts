import { useEffect } from 'react';
import { holdFrontendRelease } from '@/frontendRelease';

/** Hold the release guard while `active`: the reader has unsaved work here. */
export function useFrontendReleaseHold(active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    return holdFrontendRelease();
  }, [active]);
}
