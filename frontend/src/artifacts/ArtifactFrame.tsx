import { useEffect, useMemo, useRef, useState } from 'react';
import { useTheme } from 'next-themes';
import { listAgents } from '@/api';
import { getAssistant } from '@/assistant/api';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { ARTIFACT_SANDBOX, buildArtifactDocument } from './document';
import { ArtifactPageError, BridgeHost } from './BridgeHost';
import type { Consent, ConsentAnswer } from './BridgeHost';
import type { ArtifactKind, ArtifactRuntime, ArtifactTarget } from './types';
import { useArtifactNavigation } from './navigation';
type FrameProps = {
  runtime: ArtifactRuntime;
  target: ArtifactTarget;
  kind: ArtifactKind;
  onVersion: () => void;
  onRuntimeError?: (error: unknown) => void;
  historical?: boolean;
  pinned?: boolean;
};
export function ArtifactFrame(props: FrameProps) {
  const { resolvedTheme } = useTheme();
  const key = JSON.stringify([
    props.runtime.artifact.artifact_id,
    props.runtime.version,
    props.runtime.viewer,
    props.runtime.manifest,
    props.historical,
    props.pinned,
    props.target,
    resolvedTheme,
  ]);
  return <ArtifactFramePage key={key} {...props} />;
}
function ArtifactFramePage({
  runtime,
  target,
  kind,
  onVersion,
  onRuntimeError,
  historical = false,
  pinned = false,
}: FrameProps) {
  const { t } = useTranslation();
  const navigation = useArtifactNavigation();
  const { resolvedTheme } = useTheme();
  const [modelAgentName, setModelAgentName] = useState('');
  const [frame, setFrame] = useState<HTMLIFrameElement | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [pageError, setPageError] = useState(false);
  const [request, setRequest] = useState<Consent | null>(null);
  const queue = useRef<
    { request: Consent; resolve: (accepted: ConsentAnswer) => void }[]
  >([]);
  const active = useRef<{
    request: Consent;
    resolve: (accepted: ConsentAnswer) => void;
  } | null>(null);
  const callbacks = useRef({ onVersion, onRuntimeError });
  callbacks.current = { onVersion, onRuntimeError };
  const doc = useMemo(
    () =>
      buildArtifactDocument({
        kind,
        content: runtime.content,
        origin: window.location.origin,
        theme: resolvedTheme === 'dark' ? 'dark' : 'light',
        release:
          document
            .querySelector<HTMLScriptElement>('script[src*="assets/main-"]')
            ?.src.split('/')
            .at(-1) ?? 'development',
      }),
    [kind, runtime.content, resolvedTheme],
  );
  const [permissionSelection, setPermissionSelection] = useState<string[]>([]);
  const answer = (accepted: boolean) => {
    active.current?.resolve(
      accepted && active.current.request.kind === 'permissions'
        ? permissionSelection
        : accepted,
    );
    active.current = queue.current.shift() ?? null;
    setPermissionSelection(active.current?.request.selected ?? []);
    setRequest(active.current?.request ?? null);
  };
  useEffect(() => {
    if (!frame) return;
    let disposed = false;
    const consent = async (request: Consent) => {
      if (request.kind === 'sample') {
        const origin = runtime.artifact.origin;
        if (origin.assistant_id) {
          const assistant = await getAssistant(origin.assistant_id);
          if (disposed) return false;
          setModelAgentName(assistant.display_name || assistant.assistant_id);
        } else {
          const agents = await listAgents();
          const agent = agents.find(
            (agent) => agent.agent_id === origin.agent_id,
          );
          if (!agent) throw new Error('The origin Agent is unavailable.');
          if (disposed) return false;
          setModelAgentName(agent.name);
        }
      }
      return new Promise<ConsentAnswer>((resolve) => {
        if (active.current) queue.current.push({ request, resolve });
        else {
          active.current = { request, resolve };
          setPermissionSelection(request.selected ?? []);
          setRequest(request);
        }
      });
    };
    const host = new BridgeHost(
      frame,
      runtime,
      target,
      consent,
      (failure) => {
        setPageError(failure instanceof ArtifactPageError);
        setError(failure instanceof Error ? failure.message : String(failure));
        callbacks.current.onRuntimeError?.(failure);
      },
      () => callbacks.current.onVersion(),
      historical,
      pinned,
    );
    setReady(true);
    return () => {
      disposed = true;
      host.stop();
      setReady(false);
      active.current?.resolve(false);
      active.current = null;
      queue.current.splice(0).forEach((item) => item.resolve(false));
    };
  }, [frame]);
  const origin = runtime.artifact.origin;
  return (
    <div
      className="relative flex min-h-0 flex-1 flex-col"
      data-testid="artifact-frame-shell"
    >
      {error && (
        <ErrorNote className="absolute inset-x-3 top-3 z-10">
          <div className="flex flex-col gap-2">
            <span>{error}</span>
            {pageError && navigation.setComposer ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  navigation.setComposer!(
                    t('misc:artifacts.fix_prompt', {
                      id: runtime.artifact.artifact_id,
                      version: runtime.version,
                      error,
                    }),
                  )
                }
              >
                {t('misc:artifacts.ask_fix')}
              </Button>
            ) : origin.session_id ? (
              <a
                className="underline"
                href={`/sessions/${encodeURIComponent(origin.session_id)}`}
              >
                {t('misc:artifacts.origin')}
              </a>
            ) : origin.assistant_id ? (
              <a className="underline" href="/assistants">
                {t('misc:artifacts.origin_assistant')}
              </a>
            ) : null}
          </div>
        </ErrorNote>
      )}
      <iframe
        ref={setFrame}
        title={runtime.artifact.title}
        sandbox={ARTIFACT_SANDBOX}
        srcDoc={ready ? doc : undefined}
        className="min-h-0 w-full flex-1 border-0 bg-background"
        data-testid="artifact-frame"
      />
      <Dialog
        open={request !== null}
        onOpenChange={(open) => {
          if (!open) answer(false);
        }}
      >
        <DialogContent showCloseButton={false} className="sm:max-w-lg">
          <DialogTitle className="font-medium">
            {t(`misc:artifacts.consent_${request?.kind ?? 'sample'}`)}
          </DialogTitle>
          <DialogDescription>
            {request?.kind === 'agent'
              ? t('misc:artifacts.agent_consent_hint')
              : request?.kind === 'sample'
                ? t('misc:artifacts.sample_consent_hint', {
                    name: modelAgentName || t('misc:artifacts.origin_agent'),
                  })
                : request?.kind === 'downloads'
                  ? request.filename
                  : t('misc:artifacts.permissions_hint')}
          </DialogDescription>
          {request?.prompt !== undefined && (
            <pre
              data-slot="verbatim"
              className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-sm"
            >
              {request.prompt}
            </pre>
          )}
          {request?.names && (
            <ul className="list-inside list-disc text-sm">
              {request.names.map((name) => (
                <li key={name}>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={permissionSelection.includes(name)}
                      onChange={(event) =>
                        setPermissionSelection((current) =>
                          event.target.checked
                            ? [...current, name]
                            : current.filter((value) => value !== name),
                        )
                      }
                    />
                    {t(`misc:artifacts.capability_${name}`)}
                  </label>
                </li>
              ))}
            </ul>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => answer(false)}>
              {t('common:cancel')}
            </Button>
            <Button onClick={() => answer(true)}>{t('common:confirm')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
