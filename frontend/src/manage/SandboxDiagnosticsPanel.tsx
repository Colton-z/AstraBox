import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Copy, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ApiError, adminReadSandboxDiagnostics } from '@/api';
import type { AdminSandboxDiagnostics, AdminSandboxDiagnosticScope } from '@/types';

/** The four reports, in the order an operator reads them: what → where → why. */
const SCOPES: AdminSandboxDiagnosticScope[] = ['summary', 'inspect', 'events', 'logs'];

/**
 * What one scope's tab holds. The three failures are separate states because
 * they are separate facts: the backend produces no such report; the server
 * answered and the answer was a refusal, with its reason; nothing answered at
 * all.
 */
type Loaded =
  | { kind: 'report'; report: AdminSandboxDiagnostics }
  | { kind: 'unimplemented'; message: string }
  | { kind: 'rejected'; status: number; message: string }
  | { kind: 'unreachable'; message: string };

/**
 * Sort a failure by what the server actually said.
 *
 * `ApiError` is thrown only for a response that came back with a status, so it
 * is exactly the "the server answered" predicate; a transport failure arrives
 * as a plain `Error`. Among answers, a 501 is the only one that says anything
 * about whether the report exists — it is the backend declining to produce this
 * scope. Every other status (a 404 for the sandbox, a 502 from the runtime) is
 * a definite answer about something else, and carries the server's own reason,
 * which is worth showing instead of being flattened into "couldn't ask".
 */
function classify(error: unknown): Loaded {
  const message = (error as Error).message;
  if (!(error instanceof ApiError)) {
    return { kind: 'unreachable', message };
  }
  return error.status === 501
    ? { kind: 'unimplemented', message }
    : { kind: 'rejected', status: error.status, message };
}

/**
 * The four diagnostic reports for one sandbox.
 *
 * These reports are **plain text**, and this panel is built around that fact
 * rather than in spite of it: a mono, scrollable, selectable block that shows
 * exactly what the server rendered. There is deliberately no table, no field
 * extraction and no highlighting — the upstream format carries no schema and no
 * stability guarantee, so anything that parsed it would be presenting a
 * structure nobody agreed to, and would silently misread the day it changes.
 *
 * A backend that does not produce a report answers 501, and that is shown as
 * the stated fact it is, carrying the server's own reason. An empty panel would
 * read as a sandbox with nothing to say, which is a different claim.
 *
 * The two other ways to come back empty each get their own state instead of
 * borrowing that sentence. "This backend produces no such report", "the server
 * answered and said no, here is why" and "nothing answered" are three different
 * facts, and an operator acts on them differently: the first is a property of
 * the backend, the second names something to fix and will answer identically
 * until it is fixed, and only the third is a bare reason to retry.
 *
 * One report is fetched at a time, on demand, and kept: a report is a live read
 * against the control plane (a log pull, an event query), not free.
 */
export function SandboxDiagnosticsPanel({ sandboxId }: { sandboxId: string }) {
  const { t } = useTranslation();
  const [scope, setScope] = useState<AdminSandboxDiagnosticScope>('summary');
  const [cache, setCache] = useState<Partial<Record<string, Loaded>>>({});
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  //: Scopes already asked for. A ref, not the cache, because whether a scope
  //: has been asked for must not be part of the fetch callback's identity —
  //: deriving it from state makes the callback change the moment a report
  //: lands, which re-runs the effect and asks the control plane the same
  //: question twice.
  const requested = useRef<Set<string>>(new Set());

  // A new sandbox is a new set of reports; nothing from the last one carries.
  useEffect(() => {
    requested.current = new Set();
    setCache({});
    setScope('summary');
  }, [sandboxId]);

  const load = useCallback(
    async (wanted: AdminSandboxDiagnosticScope, { force = false } = {}) => {
      if (!sandboxId) return;
      if (!force && requested.current.has(wanted)) return;
      requested.current.add(wanted);
      setLoading(true);
      try {
        const report = await adminReadSandboxDiagnostics(sandboxId, wanted);
        setCache((prev) => ({ ...prev, [wanted]: { kind: 'report', report } }));
      } catch (e) {
        // An answer — including a refusal — is carried with the reason the
        // backend or its server gave, not flattened into an empty report. A
        // request that never landed is not an answer, and is kept apart.
        setCache((prev) => ({ ...prev, [wanted]: classify(e) }));
      } finally {
        setLoading(false);
      }
    },
    [sandboxId],
  );

  useEffect(() => {
    void load(scope);
  }, [scope, load]);

  const current = cache[scope];
  const text = current?.kind === 'report' ? current.report.text : '';

  const copy = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard may be unavailable (insecure origin, denied permission) */
    }
  };

  return (
    <div className="mt-0.5 space-y-2">
      {/* Each diagnostics scope keeps its `TabsContent` inside the same `Tabs`
          root as its trigger so Base UI can resolve the generated
          `aria-controls` and `aria-labelledby` references. Only the selected
          scope's report is mounted. */}
      <Tabs value={scope} onValueChange={(v) => setScope(v as AdminSandboxDiagnosticScope)}>
        <TabsList variant="line" className="w-full">
          {SCOPES.map((s) => (
            <TabsTrigger key={s} value={s} className="text-xs">
              {t(`manage:sandboxes.scope.${s}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        {SCOPES.map((s) => (
          <TabsContent key={s} value={s} className="mt-2 space-y-2">

      <div className="flex items-center justify-between gap-2">
        <span className="console-label text-muted-foreground/80">
          {t('manage:sandboxes.diagnostics_plaintext')}
        </span>
        {/* The card band, which is where these stand: this panel is the body of
            a ConsoleCard, whose own foot and head actions are `sm` (28px). A
            Refresh here, a Refresh in the security card beside it and a Save in
            a card's foot are one band and so take one size, on the scale
            (docs/frontend-design.md §9).

            No `aria-label`: the visible word is the name, and an aria-label
            duplicating it overrides it, so Copy would go on announcing itself
            as "Copy" after its label changed to "Copied". */}
        <span className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="disabled:opacity-100"
            onClick={() => void load(scope, { force: true })}
            disabled={loading}
          >
            <RefreshCw className={loading ? 'animate-spin' : undefined} />
            {t('common:refresh')}
          </Button>
          {text && (
            <Button variant="outline" size="sm" onClick={() => void copy()}>
              {copied ? <Check /> : <Copy />}
              {copied ? t('common:copied') : t('common:copy')}
            </Button>
          )}
        </span>
      </div>

      {/* All three failures are `Alert`, which carries the alert role: each one
          reports something that has just landed, and a failure nothing announces
          is a blank panel to a screen reader. The hue is the only difference and
          it rides on the className, as `CredentialsListPage.tsx` does —
          `variant="destructive"` would repaint title and body and drop the wash,
          and the citrine state is not a fault at all.

          Two detail lines are two `AlertDescription` rows of the Alert's grid,
          not two paragraphs inside one: `AlertDescription` spaces its own `<p>`
          children a full rem apart. */}
      {current?.kind === 'unimplemented' ? (
        // The backend answered that it produces no such report. Citrine: a fact
        // about the backend, not a fault.
        <Alert className="rounded-md border-citrine/30 bg-citrine-tint">
          <AlertTitle className="text-xs">
            {t('manage:sandboxes.diagnostics_unavailable', {
              scope: t(`manage:sandboxes.scope.${scope}`),
            })}
          </AlertTitle>
          <AlertDescription className="console-val break-words text-11 leading-relaxed">
            {current.message}
          </AlertDescription>
        </Alert>
      ) : current?.kind === 'rejected' ? (
        // The server answered, and the answer was a refusal with a reason. The
        // status is shown because it is what the operator looks up, and the
        // reason verbatim because the server states it more precisely than
        // any paraphrase would.
        <Alert className="rounded-md border-crimson/30 bg-crimson-tint">
          <AlertTitle className="text-xs">
            {t('manage:sandboxes.diagnostics_rejected', {
              scope: t(`manage:sandboxes.scope.${scope}`),
              status: current.status,
            })}
          </AlertTitle>
          <AlertDescription className="console-val break-words text-11 leading-relaxed">
            {current.message}
          </AlertDescription>
          <AlertDescription className="text-11 leading-relaxed text-muted-foreground/80">
            {t('manage:sandboxes.diagnostics_rejected_note')}
          </AlertDescription>
        </Alert>
      ) : current?.kind === 'unreachable' ? (
        // Nothing answered — the request never reached a status. Deliberately
        // not phrased as "no report": whether this sandbox has one stayed
        // unasked.
        <Alert className="rounded-md border-crimson/30 bg-crimson-tint">
          <AlertTitle className="text-xs">
            {t('manage:sandboxes.diagnostics_unreachable', {
              scope: t(`manage:sandboxes.scope.${scope}`),
            })}
          </AlertTitle>
          <AlertDescription className="console-val break-words text-11 leading-relaxed">
            {current.message}
          </AlertDescription>
          <AlertDescription className="text-11 leading-relaxed text-muted-foreground/80">
            {t('manage:sandboxes.diagnostics_unreachable_note')}
          </AlertDescription>
        </Alert>
      ) : current?.kind === 'report' ? (
        <>
          {current.report.truncated && (
            <div className="console-label text-citrine-fg">
              {t('manage:sandboxes.diagnostics_truncated')}
            </div>
          )}
          <pre tabIndex={0} className="console-scroll console-val max-h-80 select-text overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/40 px-2.5 py-2 text-11 leading-5 text-foreground">
            {current.report.text || t('manage:sandboxes.diagnostics_empty')}
          </pre>
        </>
      ) : (
        <div className="rounded-md border bg-muted/40 px-2.5 py-3 text-xs text-muted-foreground">
          {t('common:loading')}
        </div>
      )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

export default SandboxDiagnosticsPanel;
