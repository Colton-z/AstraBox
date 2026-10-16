import { Navigate, useLocation } from 'react-router-dom';

import { useAuthProbe } from '@/hooks/useAuthProbe';
import { AuthProbeFailure } from './AuthProbeFailure';

/**
 * Gate in front of the console: ask once who this browser is, then render.
 *
 * Probe failures stay on an error surface with a manual retry.
 * A deployment with no identity configured passes straight through — demanding
 * a sign-in there would ask for an account that cannot exist.
 *
 * While the answer is outstanding, the gate does not mount the console or issue
 * its requests. The busy surface keeps console content hidden until the
 * browser's identity outcome is known.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const { probe, retry } = useAuthProbe();

  if (!probe) return <div className="min-h-svh bg-background" aria-busy="true" />;
  if (probe.mode === 'error') return <AuthProbeFailure onRetry={retry} />;
  if (probe.mode === 'signed-out') {
    const next = location.pathname + location.search;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <>{children}</>;
}
