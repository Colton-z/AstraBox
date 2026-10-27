import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useSWR from 'swr';
import { ArrowLeft, ChevronRight } from 'lucide-react';

import {
  deletePersonalSchedule, listAgents, listPersonalScheduleRuns, listPersonalSchedules,
  replayPersonalScheduleRun, triggerPersonalSchedule, updatePersonalSchedule,
} from '@/api';
import { listAssistants } from '@/assistant/api';
import { ErrorNote } from '@/components/shell';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useKeepCurrent } from '@/hooks/useKeepCurrent';
import { ConsoleDangerButton, StatusPill, formatDateTime } from '@/manage/console';
import type { DeploymentRun, PersonalSchedule } from '@/types';

const RUN_LABELS: Record<DeploymentRun['status'], string> = {
  QUEUED: 'queued', RUNNING: 'running', WAITING_INPUT: 'waiting', COMPLETED: 'completed',
  FAILED: 'failed', CANCELLED: 'cancelled', UNKNOWN: 'unknown',
};

function runTone(status: DeploymentRun['status']) {
  if (status === 'COMPLETED') return 'done' as const;
  if (status === 'FAILED' || status === 'CANCELLED') return 'failed' as const;
  if (status === 'WAITING_INPUT') return 'approval' as const;
  if (status === 'RUNNING') return 'running' as const;
  return 'pending' as const;
}

type Draft = { name: string; prompt_prefix: string; cron: string; timezone: string };
function fields(plan: PersonalSchedule): Draft {
  return { name: plan.name, prompt_prefix: plan.prompt_prefix,
    cron: plan.schedule.cron, timezone: plan.schedule.timezone };
}

/**
 * Everything that runs on a schedule as the signed-in user, in the account
 * settings menu: what their Assistant runs itself and what it hands to an Agent.
 */
export function PersonalSchedulesDialog({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState('');
  const plans = useSWR(open ? 'personal-schedules' : null, listPersonalSchedules);
  const agents = useSWR(open ? 'personal-schedule-agents' : null, listAgents);
  const assistants = useSWR(open ? 'personal-schedule-assistants' : null, listAssistants);
  useEffect(() => { if (!open) setSelectedId(''); }, [open]);
  const selected = plans.data?.find((plan) => plan.deployment_id === selectedId);
  const targetName = (plan: PersonalSchedule) => (plan.assistant_id
    ? assistants.data?.find((assistant) => assistant.assistant_id === plan.assistant_id)?.display_name
      || plan.assistant_id
    : agents.data?.find((agent) => agent.agent_id === plan.agent_id)?.name || plan.agent_id || '');
  const sourceName = selected && (assistants.data?.find(
    (assistant) => assistant.assistant_id === selected.source_assistant_id,
  )?.display_name || selected.source_assistant_id);
  const error = plans.error || agents.error || assistants.error;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85dvh] flex-col gap-4 sm:max-w-2xl">
        <DialogHeader className="pr-8">
          {selectedId && (
            <Button variant="ghost" size="sm" className="w-fit" onClick={() => setSelectedId('')}>
              <ArrowLeft className="size-4" />{t('misc:personal_schedules.back')}
            </Button>
          )}
          <DialogTitle>{selected?.name || t('misc:personal_schedules.title')}</DialogTitle>
          <DialogDescription>{t('misc:personal_schedules.intro')}</DialogDescription>
        </DialogHeader>
        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
          {error && <ErrorNote>{(error as Error).message}</ErrorNote>}
          {open && selected ? (
            <ScheduleDetail
              key={selected.deployment_id} plan={selected} targetName={targetName(selected)}
              sourceName={sourceName || ''} onChanged={() => plans.mutate()}
              onDeleted={() => setSelectedId('')} onClose={() => onOpenChange(false)}
            />
          ) : selectedId ? (
            <p className="t-copy text-muted-foreground">{t('misc:personal_schedules.not_found')}</p>
          ) : plans.isLoading ? (
            <p>{t('common:loading')}</p>
          ) : plans.data?.length === 0 ? (
            <div className="flex flex-col items-start gap-3 py-6">
              <p className="t-copy text-muted-foreground">{t('misc:personal_schedules.empty')}</p>
              <Button render={<Link to="/assistants" onClick={() => onOpenChange(false)} />}>
                {t('misc:personal_schedules.ask_assistant')}
              </Button>
            </div>
          ) : (
            <ul className="flex flex-col divide-y" data-testid="personal-schedules">
              {plans.data?.map((plan) => (
                <li key={plan.deployment_id}>
                  <button type="button" aria-label={plan.name} className="flex w-full items-center gap-3 rounded-md px-2 py-3 text-left hover:bg-muted"
                    onClick={() => setSelectedId(plan.deployment_id)}>
                    <span className="flex min-w-0 flex-1 flex-col gap-1">
                      <span className="truncate font-medium">{plan.name}</span>
                      <span className="text-xs text-muted-foreground">{targetName(plan)}</span>
                      <span className="text-xs text-muted-foreground">
                        <span className="font-mono">{plan.schedule.cron}</span> · {plan.schedule.timezone}
                      </span>
                    </span>
                    {plan.enabled === false && <StatusPill tone="idle">{t('common:disabled')}</StatusPill>}
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ScheduleDetail({ plan, targetName, sourceName, onChanged, onDeleted, onClose }: {
  plan: PersonalSchedule;
  targetName: string;
  sourceName: string;
  onChanged: () => Promise<unknown>;
  onDeleted: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(() => fields(plan));
  const [baseline, setBaseline] = useState(() => fields(plan));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const history = useSWR(['personal-schedule-runs', plan.deployment_id],
    () => listPersonalScheduleRuns(plan.deployment_id));
  const runs = history.data || [];
  useKeepCurrent(() => history.mutate(), {
    follow: plan.enabled !== false || runs.some((run) =>
      ['QUEUED', 'RUNNING', 'WAITING_INPUT'].includes(run.status)),
  });
  // Revalidation updates untouched fields while retaining the user's draft.
  useEffect(() => {
    const next = fields(plan);
    setDraft((current) => {
      const merged = { ...current };
      for (const key of Object.keys(next) as Array<keyof Draft>) {
        if (current[key] === baseline[key]) merged[key] = next[key];
      }
      return merged;
    });
    setBaseline(next);
  }, [plan]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const complete = Object.values(draft).every((value) => value.trim());
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try { await action(); } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
        <dt className="text-muted-foreground">{t('misc:personal_schedules.agent')}</dt><dd className="break-words">{targetName}</dd>
        <dt className="text-muted-foreground">{t('misc:personal_schedules.source')}</dt>
        <dd><Link to={`/assistants/${plan.source_assistant_id}`} onClick={onClose} className="text-primary hover:underline">{sourceName}</Link></dd>
        <dt className="text-muted-foreground">{t('misc:personal_schedules.execution_identity')}</dt>
        <dd className="break-all">{t('misc:personal_schedules.you')} · <span className="font-mono text-xs">{plan.owner_user_id}</span></dd>
      </dl>
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill tone={plan.enabled === false ? 'idle' : 'done'}>
          {t(plan.enabled === false ? 'common:disabled' : 'common:enabled')}
        </StatusPill>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void run(async () => {
          await updatePersonalSchedule(plan.deployment_id, { enabled: plan.enabled === false });
          await onChanged();
        })}>{t(plan.enabled === false ? 'common:enable' : 'common:disable')}</Button>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void run(async () => {
          await triggerPersonalSchedule(plan.deployment_id);
          await history.mutate();
        })}>{t('manage:deployments.run_now')}</Button>
        <ConsoleDangerButton size="sm" disabled={busy} confirm={{
          title: t('misc:personal_schedules.delete_title', { name: plan.name }),
          description: t('misc:personal_schedules.delete_hint'), action: t('common:delete'),
          onConfirm: () => void run(async () => {
            await deletePersonalSchedule(plan.deployment_id); await onChanged(); onDeleted();
          }),
        }}>{t('common:delete')}</ConsoleDangerButton>
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
      <form data-testid="personal-schedule-form" className="flex flex-col gap-4" onSubmit={(event) => {
        event.preventDefault();
        void run(async () => {
          const saved = await updatePersonalSchedule(plan.deployment_id, {
            name: draft.name, prompt_prefix: draft.prompt_prefix,
            schedule: { cron: draft.cron, timezone: draft.timezone },
          });
          setDraft(fields(saved)); setBaseline(fields(saved)); await onChanged();
        });
      }}>
        <FieldGroup className="gap-4">
          {(['name', 'prompt_prefix', 'cron', 'timezone'] as const).map((key) => (
            <Field key={key}>
              <FieldLabel htmlFor={`personal-schedule-${key}`}>{t(`misc:personal_schedules.${key}`)}</FieldLabel>
              {key === 'prompt_prefix' ? (
                <Textarea id={`personal-schedule-${key}`} rows={3} required value={draft[key]} disabled={busy}
                  onChange={(event) => setDraft({ ...draft, [key]: event.target.value })} />
              ) : (
                <Input id={`personal-schedule-${key}`} required value={draft[key]} disabled={busy}
                  className={key === 'cron' ? 'font-mono' : undefined}
                  onChange={(event) => setDraft({ ...draft, [key]: event.target.value })} />
              )}
              {key === 'cron' && <FieldDescription>{t('misc:personal_schedules.cron_hint')}</FieldDescription>}
              {key === 'timezone' && <FieldDescription>{t('misc:personal_schedules.timezone_hint')}</FieldDescription>}
            </Field>
          ))}
        </FieldGroup>
        <div className="flex gap-2">
          <Button type="submit" size="sm" disabled={busy || !dirty || !complete}>{t('common:save')}</Button>
          {dirty && <Button type="button" variant="ghost" size="sm" disabled={busy}
            onClick={() => setDraft(fields(plan))}>{t('common:cancel')}</Button>}
        </div>
      </form>
      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-medium">{t('manage:deployments.runs_title')}</h2>
        {history.error && <ErrorNote>{(history.error as Error).message}</ErrorNote>}
        <Table data-testid="personal-schedule-runs">
          <TableHeader><TableRow>
            <TableHead>{t('manage:deployments.run_time')}</TableHead>
            <TableHead>{t('manage:deployments.run_trigger')}</TableHead>
            <TableHead>{t('common:status')}</TableHead>
            <TableHead>{t('manage:deployments.run_action')}</TableHead>
          </TableRow></TableHeader>
          <TableBody>{runs.map((item) => (
            <TableRow key={item.run_id} data-run-id={item.run_id}>
              <TableCell className="whitespace-normal text-xs">{formatDateTime(item.scheduled_for || item.created_at || '')}</TableCell>
              <TableCell>{t(`manage:deployments.run_trigger_${item.trigger}`)}</TableCell>
              <TableCell>
                <StatusPill tone={runTone(item.status)}>{t(`misc:personal_schedules.${RUN_LABELS[item.status]}`)}</StatusPill>
                {item.error && <ErrorNote className="max-w-56 whitespace-normal">{item.error}</ErrorNote>}
              </TableCell>
              <TableCell><div className="flex flex-wrap gap-2">
                {item.session_id && <Link to={`/sessions/${item.session_id}`} onClick={onClose}
                  className="text-xs text-primary hover:underline">{t('misc:personal_schedules.open_conversation')}</Link>}
                <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(async () => {
                  await replayPersonalScheduleRun(plan.deployment_id, item.run_id); await history.mutate();
                })}>{t('manage:deployments.replay')}</Button>
              </div></TableCell>
            </TableRow>
          ))}</TableBody>
        </Table>
        {runs.length === 0 && !history.error && <p className="text-xs text-muted-foreground">
          {history.isLoading ? t('common:loading') : t('misc:personal_schedules.runs_empty')}
        </p>}
      </section>
    </>
  );
}
