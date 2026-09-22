import { useEffect, useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { probeAuthSession, type AuthProbe } from '@/api';

/**
 * Gate in front of the console: ask once who this browser is, then render.
 *
 * The probe answers three ways and each gets its own outcome (see `AuthProbe`).
 * A deployment with no identity configured passes straight through — demanding
 * a sign-in there would ask for an account that cannot exist.
 *
 * While the answer is outstanding, the gate does not mount the console or issue
 * its requests. The busy surface keeps console content hidden until the
 * browser's identity outcome is known.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const [probe, setProbe] = useState<AuthProbe | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    probeAuthSession(controller.signal)
      .then(setProbe)
      // A probe that cannot be reached is not evidence of being signed out —
      // the network is down, or the server is restarting. Letting the console
      // render lets its own requests report what is actually wrong; a sign-in
      // page here would blame the reader for an outage.
      .catch(() => setProbe({ mode: 'no-auth' }));
    return () => controller.abort();
  }, []);

  if (!probe) return <div className="min-h-svh bg-background" aria-busy="true" />;
  if (probe.mode === 'signed-out') {
    const next = location.pathname + location.search;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <>{children}</>;
}
