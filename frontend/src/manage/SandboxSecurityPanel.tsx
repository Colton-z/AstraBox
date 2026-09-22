import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, ShieldCheck, ShieldOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { ErrorNote } from '@/components/shell';

import { ApiError, adminReadSandboxSecurity } from '@/api';
import type { AdminSandboxSecurity } from '@/types';

/**
 * What contains one sandbox, as the box itself reports it.
 *
 * This panel renders containment reported by the sandbox rather than deriving
 * it from deployment configuration. An operator needs to know whether the
 * hardened runtime, egress policy, and credential vault took effect; configured
 * inputs alone cannot prove that outcome.
 *
 * So this renders the box's own answer, and renders `available: false` as a
 * finding rather than as an empty panel. "No sidecar answered" and "this backend
 * cannot ask" are both real states an operator must be able to tell apart from
 * "contained", and an empty panel reads as the last of the three.
 */
type Loaded =
  | { kind: 'posture'; posture: AdminSandboxSecurity }
  | { kind: 'rejected'; status: number; message: string }
  | { kind: 'unreachable'; message: string };

export function SandboxSecurityPanel({ sandboxId }: { sandboxId: string }) {
  const { t } = useTranslation(['manage', 'common']);
  const [state, setState] = useState<Loaded | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setState({ kind: 'posture', posture: await adminReadSandboxSecurity(sandboxId) });
    } catch (err) {
      // A response carrying a status is the server answering; anything else
      // never reached it. The two are different facts and read differently.
      setState(
        err instanceof ApiError
          ? { kind: 'rejected', status: err.status, message: err.message }
          : { kind: 'unreachable', message: err instanceof Error ? err.message : String(err) }
      );
    } finally {
      setLoading(false);
    }
  }, [sandboxId]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="mt-0.5 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-11 text-muted-foreground">
          {t('manage:sandboxes.security_asked_of_box')}
        </span>
        {/* Button's `sm` size keeps this refresh action aligned with the one in
            SandboxDiagnosticsPanel and with the ConsoleCard action scale
            (docs/frontend-design.md §9). The shared Button also supplies the
            focus treatment required by §11; hand-rolled button styling could
            diverge in both dimensions and focus behavior. */}
        <Button
          variant="outline"
          size="sm"
          className="disabled:opacity-100"
          onClick={() => void load()}
          disabled={loading}
        >
          <RefreshCw className={loading ? 'animate-spin' : undefined} />
          {t('common:refresh')}
        </Button>
      </div>

      {state === null ? (
        <p className="text-11 text-muted-foreground">{t('common:loading')}</p>
      ) : state.kind === 'rejected' ? (
        <ErrorNote>
          {t('manage:sandboxes.security_rejected', {
            status: state.status,
            message: state.message,
          })}
        </ErrorNote>
      ) : state.kind === 'unreachable' ? (
        <ErrorNote>{state.message}</ErrorNote>
      ) : state.posture.available ? (
        <Contained posture={state.posture} />
      ) : (
        <Uncontained detail={state.posture.detail} />
      )}
    </div>
  );
}

function Uncontained({ detail }: { detail: string | null }) {
  const { t } = useTranslation(['manage']);
  return (
    // `Alert` so the finding is announced, like the two failures above it.
    // Citrine rides on the Alert rather than on the icon: Alert paints its
    // leading `<svg>` with `*:[svg]:text-current`, which outranks `text-citrine-fg`
    // on the icon itself. The title is put back on `text-foreground`, since the
    // hue is a marker on the icon and not a colour to read a sentence in.
    <Alert className="rounded-md border-citrine/40 bg-citrine/5 text-citrine-fg">
      <ShieldOff className="size-3.5" />
      <AlertTitle className="text-11 text-foreground">
        {t('manage:sandboxes.security_none')}
      </AlertTitle>
      {detail ? (
        <AlertDescription className="console-val whitespace-pre-wrap text-11">
          {detail}
        </AlertDescription>
      ) : null}
    </Alert>
  );
}

function Contained({ posture }: { posture: AdminSandboxSecurity }) {
  const { t } = useTranslation(['manage']);
  const deny = posture.default_action === 'deny';
  return (
    <div className="space-y-2">
      <p className="flex items-center gap-1.5 text-11 font-medium">
        <ShieldCheck className="size-3.5 text-mint-fg" />
        {t('manage:sandboxes.security_default_action', {
          action: posture.default_action ?? '—',
        })}
      </p>
      {/* Default-allow with an allowlist filters nothing, and an operator
          reading a list of allowed hosts would reasonably assume otherwise. */}
      {!deny ? (
        <p className="text-11 text-citrine-fg">{t('manage:sandboxes.security_default_allow_note')}</p>
      ) : null}

      <div>
        <p className="text-11 text-muted-foreground">{t('manage:sandboxes.security_egress')}</p>
        {posture.egress_rules.length ? (
          <ul className="console-val mt-0.5 space-y-0.5">
            {posture.egress_rules.map((rule) => (
              <li key={`${rule.action}:${rule.target}`} className="text-11">
                <span className={rule.action === 'allow' ? 'text-mint-fg' : 'text-destructive'}>
                  {rule.action}
                </span>{' '}
                {rule.target}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-11 text-muted-foreground">—</p>
        )}
      </div>

      <div>
        <p className="text-11 text-muted-foreground">{t('manage:sandboxes.security_vault')}</p>
        {posture.binding_names.length ? (
          <>
            <ul className="console-val mt-0.5 space-y-0.5">
              {posture.binding_names.map((name) => (
                <li key={name} className="text-11">
                  {name}
                </li>
              ))}
            </ul>
            {/* Said plainly, because it is the property the vault exists for. */}
            <p className="mt-1 text-11 text-muted-foreground">
              {t('manage:sandboxes.security_vault_note')}
            </p>
          </>
        ) : (
          <p className="text-11 text-muted-foreground">
            {t('manage:sandboxes.security_vault_empty')}
          </p>
        )}
      </div>
    </div>
  );
}
