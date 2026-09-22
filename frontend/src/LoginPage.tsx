import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';

import { AstraMark } from '@/components/AstraConsole';
import { Button } from '@/components/ui/button';
import { LanguageSwitcher } from '@/components/LanguageSwitcher';
import { probeAuthSession, signInHref, type AuthProbe } from './api';

/** Where to return after the identity provider hands the browser back. */
function nextFromSearch(search: string): string {
  const raw = new URLSearchParams(search).get('next') || '';
  // Only same-origin paths: a `next` that could be an absolute URL is an open
  // redirect, and this value reaches a Location header.
  return raw.startsWith('/') && !raw.startsWith('//') ? raw : '/';
}

/**
 * The console's own sign-in page.
 *
 * It exists so a first-time reader lands on AstraBox rather than on whichever
 * identity provider the deployment configured — they arrive knowing what this
 * is and which account to use. The page hands off to `/api/v1/auth/login` on a
 * click; it never redirects on its own, because an automatic bounce is the
 * blank flash this page replaces.
 *
 * Two states send the reader away instead of rendering: an already-valid
 * session (nothing to sign into) and a deployment with no identity configured
 * (no account to sign in with).
 */
export default function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const next = nextFromSearch(location.search);
  const [probe, setProbe] = useState<AuthProbe | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    probeAuthSession(controller.signal)
      .then(setProbe)
      // A probe that cannot be reached is not evidence of being signed out, and
      // `RequireAuth` reads it the same way — the gate lets the console render
      // so its own requests can report the outage, and a sign-in card here
      // would blame the reader for it instead.
      .catch(() => setProbe({ mode: 'no-auth' }));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (probe && probe.mode !== 'signed-out') navigate(next, { replace: true });
  }, [probe, next, navigate]);

  // Render nothing while the probe is open, and while a redirect is pending:
  // a sign-in card that appears for one frame and vanishes reads as a fault.
  if (!probe || probe.mode !== 'signed-out') {
    return <div className="min-h-svh bg-background" aria-busy="true" />;
  }

  return (
    <main className="relative flex min-h-svh flex-col items-center justify-center bg-background px-6">
      <div className="bg-starfield pointer-events-none absolute inset-0 opacity-40" aria-hidden />

      <div className="relative w-full max-w-sm">
        <div className="flex flex-col items-center gap-4 text-center">
          <div className="flex size-12 items-center justify-center rounded-xl border bg-card">
            <AstraMark size={24} />
          </div>
          <div className="flex flex-col gap-2">
            <h1 className="t-h1-tight text-2xl">{t('shell:signin.title')}</h1>
            <p className="t-copy max-w-xs text-muted-foreground">{t('shell:signin.lede')}</p>
          </div>
        </div>

        <Button
          className="mt-7 w-full"
          onClick={() => window.location.assign(signInHref(next))}
        >
          {t('shell:signin.action')}
        </Button>

        <p className="t-copy-sm mt-4 text-center text-muted-foreground">
          {t('shell:signin.note')}
        </p>
      </div>

      <div className="relative mt-10">
        <LanguageSwitcher />
      </div>
    </main>
  );
}
