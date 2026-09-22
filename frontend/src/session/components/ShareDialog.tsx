import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Copy, Share2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ErrorNote } from '@/components/shell';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  createSessionShare,
  getSessionShare,
  MODULE_BASE,
  revokeSessionShare,
  type ShareConfig,
} from '@/api';

const EXPIRY_OPTIONS: { labelKey: string; seconds: number | null }[] = [
  { labelKey: 'chat:share.expiry_never', seconds: null },
  { labelKey: 'chat:share.expiry_1d', seconds: 86400 },
  { labelKey: 'chat:share.expiry_7d', seconds: 604800 },
  { labelKey: 'chat:share.expiry_30d', seconds: 2592000 },
];

function shareUrl(token: string): string {
  return `${window.location.origin}${MODULE_BASE}/share/${token}`;
}

// Owner-side share control for a session. Generates a read-only link others can
// open after logging in. Self-contained: mounts its own trigger button.
export function ShareDialog({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [config, setConfig] = useState<ShareConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [expiry, setExpiry] = useState<string>('null');
  const [allowDownload, setAllowDownload] = useState(false);
  // The open-time config read runs while the dialog is already interactive; a
  // toggle the user has touched must not be overwritten by that read landing.
  const allowDownloadTouchedRef = useRef(false);
  const [copied, setCopied] = useState(false);

  // `SelectValue` prints the selected item's label only when the `Select` root
  // is given the item list; on its own it prints the raw value, so the trigger
  // would read `86400` instead of the translated duration. One array feeds both
  // the root and the options below it so the two spellings cannot drift apart.
  const expiryItems = useMemo(
    () => EXPIRY_OPTIONS.map((o) => ({ value: String(o.seconds), label: t(o.labelKey) })),
    [t],
  );

  useEffect(() => {
    if (!open) return;
    let alive = true;
    allowDownloadTouchedRef.current = false;
    setLoading(true);
    void (async () => {
      try {
        const c = await getSessionShare(sessionId);
        if (!alive) return;
        setConfig(c);
        if (!allowDownloadTouchedRef.current) {
          setAllowDownload(Boolean(c.allow_download));
        }
        setError('');
      } catch (e) {
        if (alive) setError((e as Error).message);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [open, sessionId]);

  const enabled = Boolean(config?.enabled && config?.token);

  const enableShare = async () => {
    setLoading(true);
    setError('');
    try {
      const seconds = expiry === 'null' ? null : Number(expiry);
      setConfig(await createSessionShare(sessionId, { expires_in_seconds: seconds, allow_download: allowDownload }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const revoke = async () => {
    setLoading(true);
    setError('');
    try {
      await revokeSessionShare(sessionId);
      setConfig({ ...(config as ShareConfig), enabled: false });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const copy = async () => {
    if (!config?.token) return;
    try {
      await navigator.clipboard.writeText(shareUrl(config.token));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard may be unavailable */
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="secondary" size="sm" />}>
        <Share2 className="size-4" />
        {t('chat:share.button')}
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t('chat:share.title')}</DialogTitle>
          <DialogDescription>
            {t('chat:share.description')}
          </DialogDescription>
        </DialogHeader>

        {error && (
          <ErrorNote>
            {error}
          </ErrorNote>
        )}

        {enabled ? (
          <div className="space-y-4">
            <div className="space-y-1.5">
              {/* `leading-5` is the dialog's line height: on its own `text-xs`
                  the eyebrow's row is 16px and the control under it rides
                  4px up. */}
              <Label className="text-xs leading-5 font-medium text-muted-foreground">
                {t('chat:share.link_label')}
              </Label>
              <div className="flex items-center gap-1.5">
                <Input readOnly value={shareUrl(config!.token)} className="h-9 text-xs" />
                <Button size="icon" variant="outline" className="size-9 shrink-0" onClick={() => void copy()}>
                  <Copy className="size-4" />
                </Button>
              </div>
              {copied && <p className="text-11 text-mint-fg">{t('chat:share.copied')}</p>}
            </div>
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{t('chat:share.allow_download_status', { value: config!.allow_download ? t('common:yes') : t('common:no') })}</span>
              <span>{config!.expires_at ? t('chat:share.expires_on', { datetime: new Date(config!.expires_at).toLocaleString() }) : t('chat:share.expiry_never')}</span>
            </div>
            <div className="flex justify-end">
              <Button variant="destructive" size="sm" disabled={loading} onClick={() => void revoke()}>
                {loading ? t('chat:share.revoking') : t('chat:share.revoke')}
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="space-y-1.5">
              {/* `leading-5` as above: the dialog's line height, not the 16px
                  this box would take from its own `text-xs`. */}
              <Label className="text-xs leading-5 font-medium">{t('chat:share.validity')}</Label>
              {/* The menu hangs off the trigger's bottom-left corner rather
                  than sitting on top of it: `alignItemWithTrigger` is the macOS
                  native-select placement, which lines the SELECTED ITEM's text
                  up with the trigger's text and leaves the two boxes offset — a
                  menu that reads as having missed. */}
              <Select
                value={expiry}
                items={expiryItems}
                onValueChange={(next) => {
                  // The value is nullable for selects that carry a null option.
                  // This one does not: "never" is the string 'null'.
                  if (next === null) return;
                  setExpiry(next);
                }}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {expiryItems.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">{t('chat:share.allow_download')}</div>
                <p className="text-11 text-muted-foreground">{t('chat:share.allow_download_hint')}</p>
              </div>
              <Switch
                checked={allowDownload}
                onCheckedChange={(value) => {
                  allowDownloadTouchedRef.current = true;
                  setAllowDownload(value);
                }}
              />
            </div>
            <div className="flex justify-end">
              <Button size="sm" disabled={loading} onClick={() => void enableShare()}>
                {loading ? t('chat:share.generating') : t('chat:share.generate')}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
