import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useSWR from 'swr';
import type { BundledLanguage } from 'shiki';
import { Button } from '@/components/ui/button';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { MarkdownContent } from '@/session/components/MessageParts';
import { CodeBlock } from '@/components/ai-elements/code-block';
import { ArtifactFrame } from './ArtifactFrame';
import { artifactsApi, subscribeArtifactLive } from './api';
import { saveArtifactFile } from './BridgeHost';
import { ShareDialog } from './ShareDialog';
import type { ArtifactTarget } from './types';
export function ArtifactViewer({
  target,
  fullPage = false,
  onRuntimeError,
}: {
  target: ArtifactTarget;
  fullPage?: boolean;
  onRuntimeError?: (error: unknown) => void;
}) {
  const { t } = useTranslation();
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [actionError, setActionError] = useState('');
  const [copiedSource, setCopiedSource] = useState(false);
  const {
    data: current,
    error,
    mutate,
  } = useSWR(
    ['artifact-runtime', target.id, target.token],
    () => artifactsApi.runtime(target),
    { shouldRetryOnError: false },
  );
  const {
    data: versions,
    error: versionsError,
    mutate: refreshVersions,
  } = useSWR(target.id ? ['artifact-versions', target.id] : null, () =>
    artifactsApi.versions(target.id!),
  );
  const { data: historical, error: versionError } = useSWR(
    target.id && selectedVersion !== null
      ? ['artifact-version', target.id, selectedVersion]
      : null,
    () => artifactsApi.version(target.id!, selectedVersion!),
  );
  const refresh = useCallback(() => {
    void mutate();
    void refreshVersions();
  }, [mutate, refreshVersions]);
  useEffect(() => {
    if (error) onRuntimeError?.(error);
  }, [error, onRuntimeError]);
  const old = selectedVersion !== null && selectedVersion !== current?.version;
  const view = useMemo(
    () =>
      current &&
      (!old
        ? current
        : historical
          ? {
              ...current,
              content: historical.content,
              version: historical.version,
              manifest: historical.manifest,
              artifact: {
                ...current.artifact,
                title: historical.title,
                kind: historical.kind,
                language: historical.language,
              },
            }
          : null),
    [current, old, historical],
  );
  const pinned = Boolean(
    target.token && current && current.artifact.share?.version !== null,
  );
  const native = view && ['markdown', 'code'].includes(view.artifact.kind);
  useEffect(() => {
    if (!native || !current || old) return;
    return subscribeArtifactLive(
      target,
      (event, payload) => {
        if (
          event === 'version' &&
          !pinned &&
          Number(payload.version) > current.version
        )
          refresh();
      },
      (failure) => {
        setActionError((failure as Error).message);
        onRuntimeError?.(failure);
      },
    );
  }, [
    native,
    current?.artifact.artifact_id,
    current?.version,
    target.id,
    target.token,
    old,
    pinned,
    refresh,
    onRuntimeError,
  ]);
  if (error)
    return (
      <div className="flex flex-col gap-3 p-4">
        <ErrorNote>{error.message}</ErrorNote>
        <Button variant="outline" onClick={() => void mutate()}>
          {t('common:retry')}
        </Button>
      </div>
    );
  if (!current)
    return (
      <div className="p-4 text-sm text-muted-foreground" role="status">
        {t('common:loading')}
      </div>
    );
  const artifact = current.artifact;
  const act = async (fn: () => Promise<unknown>) => {
    setActionError('');
    try {
      await fn();
    } catch (failure) {
      setActionError((failure as Error).message);
    }
  };
  const extension: Record<string, string> = {
    html: 'html',
    react: 'tsx',
    svg: 'svg',
    markdown: 'md',
    mermaid: 'mmd',
    code: artifact.language || 'txt',
  };
  return (
    <div
      className="flex h-full min-h-0 flex-1 flex-col gap-0"
      data-testid="artifact-viewer"
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
        <h1
          className={`min-w-0 flex-1 truncate font-medium ${fullPage ? 'text-2xl' : 'text-sm'}`}
        >
          {view?.artifact.title ?? artifact.title}
        </h1>
        {versions && versions.length > 1 && (
          <label className="flex items-center gap-2 text-xs">
            <span className="sr-only">
              {t('misc:artifacts.version_history')}
            </span>
            <select
              className="console-input max-w-36 text-xs"
              value={selectedVersion ?? current.version}
              onChange={(event) =>
                setSelectedVersion(
                  Number(event.target.value) === current.version
                    ? null
                    : Number(event.target.value),
                )
              }
            >
              {versions.map((version) => (
                <option key={version.version} value={version.version}>
                  {t('misc:artifacts.version', { version: version.version })}
                </option>
              ))}
            </select>
          </label>
        )}
        {target.id && !fullPage && (
          <Button
            variant="outline"
            size={fullPage ? 'default' : 'sm'}
            render={<Link to={`/artifacts/${target.id}`} />}
          >
            {t('misc:artifacts.full_page')}
          </Button>
        )}
        {target.id && (
          <Button
            variant="outline"
            size={fullPage ? 'default' : 'sm'}
            disabled={!view}
            onClick={() =>
              void act(async () => {
                await navigator.clipboard.writeText(view!.content);
                setCopiedSource(true);
                setTimeout(() => setCopiedSource(false), 2000);
              })
            }
          >
            {t(copiedSource ? 'common:copied' : 'misc:artifacts.copy_source')}
          </Button>
        )}
        {target.id && (
          <Button
            variant="outline"
            size={fullPage ? 'default' : 'sm'}
            disabled={!view}
            onClick={() =>
              saveArtifactFile(
                `${artifact.title.replace(/[\\/:*?"<>|]/g, '-')}.${extension[view!.artifact.kind]}`,
                view!.content,
              )
            }
          >
            {t('misc:artifacts.download_source')}
          </Button>
        )}
        {target.id && (
          <Button
            variant="outline"
            size={fullPage ? 'default' : 'sm'}
            onClick={() => setShareOpen(true)}
          >
            {t('misc:artifacts.share')}
          </Button>
        )}
      </div>
      {(versionError || versionsError || actionError) && (
        <div className="p-3">
          <ErrorNote>
            {versionError?.message || versionsError?.message || actionError}
          </ErrorNote>
        </div>
      )}
      {view ? (
        native ? (
          <div className="min-h-0 flex-1 overflow-auto p-4">
            {view.artifact.kind === 'markdown' ? (
              <MarkdownContent text={view.content} />
            ) : (
              <CodeBlock
                code={view.content}
                language={(view.artifact.language ?? 'text') as BundledLanguage}
              />
            )}
          </div>
        ) : (
          <ArtifactFrame
            key={view.version}
            runtime={view}
            target={target}
            kind={view.artifact.kind}
            historical={old}
            pinned={pinned}
            onVersion={refresh}
            onRuntimeError={onRuntimeError}
          />
        )
      ) : (
        <div className="p-4" role="status">
          {t('common:loading')}
        </div>
      )}
      {target.id && (
        <ShareDialog
          artifact={artifact}
          currentManifest={current.manifest}
          version={view?.version ?? current.version}
          open={shareOpen}
          onOpenChange={setShareOpen}
        />
      )}
    </div>
  );
}
