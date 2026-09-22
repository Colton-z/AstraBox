import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Ellipsis } from '@/components/shell';
import type { RetryIntent } from '../utils/chatHelpers';

// Error/retry banner shown under the header. The parent gates rendering on
// `combinedShowRetryBanner`; this component owns only the banner body.
export function SessionRetryBanner({
  showTransientBackendMessage,
  retryIntent,
  bannerErrorMessage,
  showRetryButton,
  handleRetry,
}: {
  showTransientBackendMessage: boolean;
  retryIntent: RetryIntent;
  bannerErrorMessage: string;
  showRetryButton: boolean;
  handleRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 border-b border-destructive/30 bg-destructive/5 text-sm">
      <div className="flex flex-col gap-0.5 min-w-0 text-xs">
        {showTransientBackendMessage ? (
          <span className="text-muted-foreground">{t('chat:banner.backend_unavailable')}</span>
        ) : (
          <>
            {retryIntent.kind !== 'none' && <span className="font-medium text-destructive">{retryIntent.summary}</span>}
            {bannerErrorMessage && (
              <Ellipsis className="text-muted-foreground">{bannerErrorMessage}</Ellipsis>
            )}
          </>
        )}
      </div>
      {showRetryButton && (
        <Button variant="secondary" size="sm" className="shrink-0" onClick={handleRetry}>
          {'label' in retryIntent ? retryIntent.label : t('common:retry')}
        </Button>
      )}
    </div>
  );
}
