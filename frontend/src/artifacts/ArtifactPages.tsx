import { useEffect, useState, useCallback, useMemo } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import useSWR from 'swr';
import useSWRInfinite from 'swr/infinite';
import { useTranslation } from 'react-i18next';
import { Lock } from 'lucide-react';
import { ApiError } from '@/api';
import { AppTopbar, PageShell } from '@/components/shell';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { artifactsApi } from './api';
import { ArtifactViewer } from './ArtifactViewer';
export function ArtifactGallery() {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const {
    data: pages,
    error,
    size,
    setSize,
    isValidating,
    mutate,
  } = useSWRInfinite(
    (index, previous) =>
      previous && !previous.has_more
        ? null
        : ['artifact-gallery', query, index, previous?.next_cursor],
    ([, q, , cursor]) =>
      artifactsApi.list({
        q: String(q),
        ...(cursor ? { cursor: String(cursor) } : {}),
      }),
  );
  const artifacts = pages?.flatMap((page) => page.items) ?? [];
  return (
    <div className="flex h-full min-h-0 flex-col">
      <AppTopbar
        crumbs={[{ label: t('misc:artifacts.title') }]}
        trailLabel={t('shell:breadcrumb_navigation')}
      />
      <PageShell measure="wide">
        <h1 className="text-2xl font-medium">{t('misc:artifacts.title')}</h1>
        {(artifacts.length > 12 || query) && (
          <label className="flex flex-col gap-2 text-sm">
            {t('misc:artifacts.search')}
            <input
              className="console-input"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        )}
        {error && (
          <ErrorNote>
            {error.message}
            <Button variant="outline" onClick={() => void mutate()}>
              {t('common:retry')}
            </Button>
          </ErrorNote>
        )}
        {!pages && !error && (
          <p role="status" className="text-sm text-muted-foreground">
            {t('common:loading')}
          </p>
        )}
        {pages && !artifacts.length && (
          <p className="text-sm text-muted-foreground">
            {query
              ? t('misc:artifacts.no_matches')
              : t('misc:artifacts.empty_gallery')}
          </p>
        )}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {artifacts.map((artifact) => (
            <Link
              key={artifact.artifact_id}
              to={`/artifacts/${artifact.artifact_id}`}
              className="flex flex-col gap-2 rounded-md border border-border bg-card p-4 hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
            >
              <h2 className="truncate text-base font-medium">
                {artifact.title}
              </h2>
              {artifact.description && (
                <p className="line-clamp-2 text-sm text-muted-foreground">
                  {artifact.description}
                </p>
              )}
              <span className="text-xs text-muted-foreground">
                {t(`misc:artifacts.kind_${artifact.kind}`)} ·{' '}
                {t('misc:artifacts.version', {
                  version: artifact.current_version,
                })}
              </span>
            </Link>
          ))}
        </div>
        {pages?.at(-1)?.has_more && (
          <Button
            variant="outline"
            disabled={isValidating}
            onClick={() => void setSize(size + 1)}
          >
            {t('misc:artifacts.more')}
          </Button>
        )}
      </PageShell>
    </div>
  );
}
export function ArtifactPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { data: artifact, error } = useSWR(['artifact-record', id], () =>
    artifactsApi.get(id),
  );
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const remove = async () => {
    setBusy(true);
    try {
      await artifactsApi.delete(id);
      navigate('/artifacts');
    } catch (failure) {
      setDeleteError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex h-full min-h-0 flex-col">
      <AppTopbar
        crumbs={[
          { label: t('misc:artifacts.title'), to: '/artifacts' },
          { label: artifact?.title ?? t('misc:artifacts.record') },
        ]}
        trailLabel={t('shell:breadcrumb_navigation')}
      />
      {error ? (
        <div className="p-4">
          <ErrorNote>{error.message}</ErrorNote>
        </div>
      ) : !artifact ? (
        <p className="p-4" role="status">
          {t('common:loading')}
        </p>
      ) : (
        <>
          <div className="flex items-center justify-end gap-3 border-b border-border px-4 py-2">
            {artifact.origin.session_id ? (
              <Link
                className="text-sm underline"
                to={`/sessions/${artifact.origin.session_id}`}
              >
                {t('misc:artifacts.origin')}
              </Link>
            ) : (
              artifact.origin.assistant_id && (
                <Link className="text-sm underline" to="/assistants">
                  {t('misc:artifacts.origin_assistant')}
                </Link>
              )
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setDeleting(true)}
            >
              {t('common:delete')}
            </Button>
          </div>
          <ArtifactViewer key={id} target={{ id }} fullPage />
        </>
      )}
      <Dialog open={deleting} onOpenChange={setDeleting}>
        <DialogContent>
          <DialogTitle className="font-medium">
            {t('common:confirm_delete')}
          </DialogTitle>
          <DialogDescription>
            {t('misc:artifacts.delete_hint', { title: artifact?.title })}
          </DialogDescription>
          {deleteError && <ErrorNote>{deleteError}</ErrorNote>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(false)}>
              {t('common:cancel')}
            </Button>
            <Button disabled={busy} onClick={() => void remove()}>
              {t('common:delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
export function ArtifactSharePage() {
  const { token = '' } = useParams();
  const target = useMemo(() => ({ token, guestId: crypto.randomUUID() }), [token]);
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [error, setError] = useState<unknown>(null);
  const handleError = useCallback(
    (failure: unknown) => {
      if (
        failure instanceof ApiError &&
        failure.status === 401 &&
        failure.code === 'ARTIFACT_SIGN_IN_REQUIRED'
      )
        navigate(
          `/login?next=${encodeURIComponent(`/share/artifacts/${token}`)}`,
          { replace: true },
        );
      else if (
        failure instanceof ApiError &&
        [401, 403, 404].includes(failure.status)
      )
        setError(failure);
    },
    [navigate, token],
  );
  useEffect(() => {
    setError(null);
  }, [token]);
  if (error)
    return (
      <div className="flex h-svh flex-col items-center justify-center gap-3 px-6 text-center">
        <Lock className="size-8 text-muted-foreground" />
        <h1 className="text-base font-medium">{t('misc:share.cannot_open')}</h1>
        <p className="max-w-sm text-sm text-muted-foreground">
          {t('misc:share.link_invalid')}
        </p>
      </div>
    );
  return (
    <main className="flex h-svh min-h-0 flex-col bg-background text-foreground">
      <p className="border-b border-border px-4 py-2 text-xs text-muted-foreground">
        {t('misc:artifacts.unverified')}
      </p>
      <ArtifactViewer
        key={token}
        target={target}
        fullPage
        onRuntimeError={handleError}
      />
    </main>
  );
}
