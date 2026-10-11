import { useEffect, useState } from 'react';
import useSWR from 'swr';
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
import { MODULE_BASE } from '@/api';
import { artifactsApi } from './api';
import type { ArtifactRuntimeRecord, Manifest } from './types';
export function ShareDialog({
  artifact,
  currentManifest,
  version,
  open,
  onOpenChange,
}: {
  artifact: Pick<ArtifactRuntimeRecord, 'artifact_id' | 'title'>;
  currentManifest: Manifest;
  version: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const {
    data: share,
    error: loadError,
    mutate,
  } = useSWR(open ? ['artifact-share', artifact.artifact_id] : null, () =>
    artifactsApi.share(artifact.artifact_id),
  );
  const [access, setAccess] = useState<'view' | 'interact'>('view');
  const [audience, setAudience] = useState<'anyone' | 'signed_in'>('anyone');
  const [pin, setPin] = useState(false);
  const [expiry, setExpiry] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const restricted =
    Object.hasOwn(currentManifest, 'sample') ||
    Object.hasOwn(currentManifest, 'agent');
  useEffect(() => {
    if (share) {
      setAccess(share.access);
      setAudience(restricted ? 'signed_in' : share.audience);
      setPin(share.version !== null);
      setExpiry(share.enabled && share.expires_at ? 'preserve' : '');
    } else if (restricted) setAudience('signed_in');
  }, [share, restricted]);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      await mutate();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const link =
    share?.enabled && share.token
      ? `${window.location.origin}${MODULE_BASE}/share/artifacts/${share.token}`
      : '';
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogTitle className="font-medium">
          {t('misc:artifacts.share')}
        </DialogTitle>
        <DialogDescription>{artifact.title}</DialogDescription>
        {(error || loadError) && (
          <ErrorNote>{error || loadError.message}</ErrorNote>
        )}
        <label className="flex flex-col gap-2 text-sm">
          {t('misc:artifacts.access')}
          <select
            className="console-input"
            value={access}
            onChange={(event) => setAccess(event.target.value as typeof access)}
          >
            <option value="view">{t('misc:artifacts.access_view')}</option>
            <option value="interact">
              {t('misc:artifacts.access_interact')}
            </option>
          </select>
        </label>
        <label className="flex flex-col gap-2 text-sm">
          {t('misc:artifacts.audience')}
          <select
            className="console-input"
            value={audience}
            onChange={(event) =>
              setAudience(event.target.value as typeof audience)
            }
            aria-describedby={
              restricted ? 'artifact-audience-restriction' : undefined
            }
          >
            <option value="anyone" disabled={restricted}>
              {t('misc:artifacts.anyone')}
            </option>
            <option value="signed_in">{t('misc:artifacts.signed_in')}</option>
          </select>
        </label>
        {restricted && (
          <p
            id="artifact-audience-restriction"
            className="text-sm text-muted-foreground"
          >
            {t('misc:artifacts.audience_restricted')}
          </p>
        )}
        <label className="flex flex-col gap-2 text-sm">
          {t('misc:artifacts.shared_version')}
          <select
            className="console-input"
            value={pin ? 'pin' : 'latest'}
            onChange={(event) => setPin(event.target.value === 'pin')}
          >
            <option value="latest">{t('misc:artifacts.follow_latest')}</option>
            <option value="pin">
              {t('misc:artifacts.pin_version', { version })}
            </option>
          </select>
        </label>
        <label className="flex flex-col gap-2 text-sm">
          {t('misc:artifacts.expiry')}
          <select
            className="console-input"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
          >
            {share?.enabled && share.expires_at && (
              <option value="preserve">
                {t('misc:artifacts.keep_expiry')}
              </option>
            )}
            <option value="">{t('misc:artifacts.no_expiry')}</option>
            <option value="86400">{t('misc:artifacts.one_day')}</option>
            <option value="604800">{t('misc:artifacts.seven_days')}</option>
            <option value="2592000">{t('misc:artifacts.thirty_days')}</option>
          </select>
        </label>
        {share?.expires_at && (
          <p className="text-sm text-muted-foreground">
            {t('misc:artifacts.expires_at', {
              date: new Date(share.expires_at).toLocaleString(),
            })}
          </p>
        )}
        {link && (
          <div className="flex items-center gap-2">
            <input
              className="console-input min-w-0 flex-1"
              aria-label={t('misc:artifacts.share_link')}
              readOnly
              value={link}
            />
            <Button
              variant="outline"
              onClick={() =>
                void act(async () => {
                  await navigator.clipboard.writeText(link);
                  setCopied(true);
                })
              }
            >
              {t(copied ? 'common:copied' : 'common:copy')}
            </Button>
          </div>
        )}
        <DialogFooter>
          {link && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                void act(() => artifactsApi.revokeShare(artifact.artifact_id))
              }
            >
              {t('misc:artifacts.revoke')}
            </Button>
          )}
          <Button
            disabled={busy || !share || Boolean(loadError)}
            onClick={() =>
              void act(() =>
                artifactsApi.createShare(artifact.artifact_id, {
                  access,
                  audience,
                  version: pin ? version : null,
                  ...(expiry
                    ? {
                        expires_in_seconds:
                          expiry === 'preserve'
                            ? Math.max(
                                1,
                                Math.ceil(
                                  (Date.parse(share!.expires_at!) -
                                    Date.now()) /
                                    1000,
                                ),
                              )
                            : Number(expiry),
                      }
                    : {}),
                }),
              )
            }
          >
            {busy ? t('common:loading') : t('common:save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
