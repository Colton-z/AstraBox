import { useEffect, useState } from 'react';

import { probeAuthSession, type AuthProbe } from '@/api';

/** Read the browser's identity again only when the reader asks to retry. */
export function useAuthProbe() {
  const [probe, setProbe] = useState<AuthProbe | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void probeAuthSession(controller.signal).then((result) => {
      if (!controller.signal.aborted) setProbe(result);
    }).catch(() => {
      if (!controller.signal.aborted) setProbe({ mode: 'error' });
    });
    return () => controller.abort();
  }, [attempt]);

  return {
    probe,
    retry: () => {
      setProbe(null);
      setAttempt((value) => value + 1);
    },
  };
}
