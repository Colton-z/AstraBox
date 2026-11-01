import { skillSourceLabel } from '@/types';
import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useSWR, { useSWRConfig } from 'swr';
import { UserRound } from 'lucide-react';

import { ErrorNote } from '@/components/shell';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { listAssistantConnectors, listConnectors, setAssistantConnector } from '../api';
import { useExtensionProvider } from '@/hooks/useExtensionProvider';

import { deleteAssistant, getAssistant, updateAssistant } from './api';

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="t-eyebrow text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

function Names({ names, empty }: { names: string[]; empty: string }) {
  if (names.length === 0) return <p className="text-13 text-muted-foreground">{empty}</p>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {names.map((name) => (
        <li key={name}><Badge variant="secondary" className="font-normal">{name}</Badge></li>
      ))}
    </ul>
  );
}

/**
 * An Assistant's profile, opened from its conversation.
 *
 * What is the owner's to say lives here: its name, the timezone it keeps time
 * in, and instructions that follow the organization's. Beside that, what it
 * can use: the apps the owner connected, turned on or off for it, and what the
 * deployment's Assistant defaults provide, which the owner reads but does not
 * edit. Skills, tools and native options of its own are authored on its
 * console page.
 *
 * What the defaults select from the extension catalog is listed as provided
 * only while the deployment runs a catalog, which the same provider read as the
 * console's says without asking any catalog; without one it is reported as
 * unavailable, since every Assistant's new conversations are then refused.
 */
export function AssistantProfile({ assistantId }: { assistantId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { mutate } = useSWRConfig();
  const [open, setOpen] = useState(false);
  const assistant = useSWR(open ? ['assistant', assistantId] : null, () => getAssistant(assistantId));
  const connectors = useSWR(open ? 'connectors' : null, listConnectors);
  const enabled = useSWR(open ? ['assistant-connectors', assistantId] : null, () => listAssistantConnectors(assistantId));
  const extensionProvider = useExtensionProvider(open);
  const [name, setName] = useState('');
  const [timezone, setTimezone] = useState('');
  const [instructions, setInstructions] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const record = assistant.data;
  useEffect(() => {
    if (!record) return;
    setName(record.display_name);
    setTimezone(record.timezone ?? '');
    setInstructions(record.system ?? '');
  }, [record]);

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      await updateAssistant(assistantId, {
        display_name: name.trim(),
        timezone: timezone.trim() || null,
        system: instructions,
      });
      await Promise.all([assistant.mutate(), mutate('assistants:rail')]);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setSaving(false);
    }
  };

  // Applied to the running workspace as its profile is prepared again; the
  // record says what the owner chose.
  const setProactive = async (proactive: boolean) => {
    setError('');
    try {
      await assistant.mutate(await updateAssistant(assistantId, { proactive }), { revalidate: false });
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const toggle = async (connectorId: string, on: boolean) => {
    setError('');
    try {
      await enabled.mutate(await setAssistantConnector(assistantId, connectorId, on), { revalidate: false });
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const remove = async () => {
    setError('');
    try {
      await deleteAssistant(assistantId);
      setOpen(false);
      // A user always has a primary Assistant; asking again makes the next one.
      await Promise.all([mutate('assistant:primary'), mutate('assistants:rail')]);
      navigate('/agents');
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const on = new Set(enabled.data ?? []);
  const connected = (connectors.data ?? []).filter((c) => c.status === 'connected');
  const inherited = record?.inherited;
  const readError = (
    assistant.error ?? connectors.error ?? enabled.error ?? extensionProvider.error
  ) as Error | undefined;
  // Undefined until the provider read lands; the catalog's part is then left
  // out rather than claimed either way.
  const catalogRuns = extensionProvider.provider === undefined ? undefined : extensionProvider.provider !== null;
  const inheritedNames = Array.from(new Set([
    ...(inherited?.skills ?? []).map(skillSourceLabel),
    ...(catalogRuns ? inherited?.catalog_skills ?? [] : []).map(skillSourceLabel),
    ...(inherited?.mcp_servers ?? []),
  ]));
  const catalogSelections = (inherited?.catalog_mcp_servers ?? 0) + (inherited?.catalog_skills?.length ?? 0);
  const dirty = record !== undefined && (
    name.trim() !== record.display_name
    || (timezone.trim() || null) !== (record.timezone ?? null)
    || instructions !== (record.system ?? '')
  );

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger render={<Button variant="outline" size="sm" data-testid="assistant-profile-open" />}>
        <UserRound />
        {t('chat:assistant_profile.open')}
      </SheetTrigger>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-md" data-testid="assistant-profile">
        <SheetHeader className="border-b">
          <SheetTitle>{record?.display_name ?? t('common:loading')}</SheetTitle>
          <SheetDescription>{t('chat:assistant_profile.description')}</SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-6 p-4">
          {(error || readError) && (
            <ErrorNote title={t('chat:assistant_profile.failed')}>{error || readError?.message}</ErrorNote>
          )}
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="assistant-profile-name">{t('chat:assistant_profile.name')}</FieldLabel>
              <Input id="assistant-profile-name" value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field>
              <FieldLabel htmlFor="assistant-profile-timezone">{t('chat:assistant_profile.timezone')}</FieldLabel>
              <Input
                id="assistant-profile-timezone"
                value={timezone}
                placeholder="Asia/Shanghai"
                onChange={(e) => setTimezone(e.target.value)}
              />
              <FieldDescription>{t('chat:assistant_profile.timezone_hint')}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="assistant-profile-instructions">{t('chat:assistant_profile.instructions')}</FieldLabel>
              <Textarea
                id="assistant-profile-instructions"
                rows={5}
                value={instructions}
                onChange={(e) => setInstructions(e.target.value)}
              />
              <FieldDescription>
                {inherited?.instructions
                  ? t('chat:assistant_profile.instructions_after_org')
                  : t('chat:assistant_profile.instructions_hint')}
              </FieldDescription>
            </Field>
            <div>
              <Button size="sm" disabled={!dirty || saving || !name.trim()} onClick={() => { void save(); }}>
                {saving ? t('chat:assistant_profile.saving') : t('common:save')}
              </Button>
            </div>
          </FieldGroup>

          <Section title={t('chat:assistant_profile.connections')}>
            {connected.length === 0 ? (
              <p className="text-13 text-muted-foreground">{t('chat:assistant_profile.no_connections')}</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {connected.map((connector) => (
                  <li key={connector.connector_id} className="flex items-center justify-between gap-3">
                    <span className="text-13">{connector.name}</span>
                    <Switch
                      checked={on.has(connector.connector_id)}
                      aria-label={connector.name}
                      onCheckedChange={(checked) => { void toggle(connector.connector_id, checked); }}
                    />
                  </li>
                ))}
              </ul>
            )}
            <Link to="/connections" className="text-13 text-primary underline-offset-4 hover:underline">
              {t('chat:assistant_welcome.open_connections')}
            </Link>
          </Section>

          <Section title={t('chat:assistant_profile.proactive')}>
            <div className="flex items-start justify-between gap-3">
              <p className="text-13 text-muted-foreground">{t('chat:assistant_profile.proactive_hint')}</p>
              <Switch
                data-testid="assistant-proactive"
                checked={record?.proactive !== false}
                disabled={record === undefined}
                aria-label={t('chat:assistant_profile.proactive')}
                onCheckedChange={(checked) => { void setProactive(checked); }}
              />
            </div>
          </Section>

          <Section title={t('chat:assistant_profile.from_organization')}>
            <Names names={inheritedNames} empty={t('chat:assistant_profile.nothing_inherited')} />
            {catalogRuns === true && (inherited?.catalog_mcp_servers ?? 0) > 0 && (
              <p className="text-13 text-muted-foreground">
                {t('chat:assistant_profile.catalog_tools', { count: inherited?.catalog_mcp_servers ?? 0 })}
              </p>
            )}
            {catalogRuns === false && catalogSelections > 0 && (
              <p className="text-13 text-muted-foreground" data-testid="assistant-profile-catalog-unavailable">
                {t('chat:assistant_profile.catalog_unavailable', { count: catalogSelections })}
              </p>
            )}
          </Section>

          <Section title={t('chat:assistant_profile.its_own')}>
            <Names
              names={[...(record?.skills ?? []).map(skillSourceLabel), ...Object.keys(record?.mcp_servers ?? {})]}
              empty={t('chat:assistant_profile.nothing_own')}
            />
            <Link
              to={`/manage/assistants/${encodeURIComponent(assistantId)}`}
              className="text-13 text-primary underline-offset-4 hover:underline"
            >
              {t('chat:assistant_profile.advanced')}
            </Link>
          </Section>

          <Section title={t('chat:assistant_profile.delete_title')}>
            <AlertDialog>
              <AlertDialogTrigger render={<Button variant="outline" size="sm" className="self-start text-destructive" />}>
                {t('common:delete')}
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>{t('chat:assistant_profile.delete_title')}</AlertDialogTitle>
                  <AlertDialogDescription>
                    {t('chat:assistant_profile.delete_confirm', { name: record?.display_name ?? '' })}
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>{t('common:cancel')}</AlertDialogCancel>
                  <AlertDialogAction variant="destructive" onClick={() => { void remove(); }}>
                    {t('common:delete')}
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </Section>
        </div>
      </SheetContent>
    </Sheet>
  );
}
