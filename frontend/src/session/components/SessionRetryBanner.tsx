import { useTranslation } from 'react-i18next';
import { Ellipsis } from '@/components/shell';

// The banner under the header: the session's stream cannot be reached, or its
// detail could not be read. The parent gates rendering; this component owns
// only the banner body.
export function SessionRetryBanner({
  showTransientBackendMessage,
  bannerErrorMessage,
}: {
  showTransientBackendMessage: boolean;
  bannerErrorMessage: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 border-b border-destructive/30 bg-destructive/5 text-sm">
      <div className="flex flex-col gap-0.5 min-w-0 text-xs">
        {showTransientBackendMessage ? (
          <span className="text-muted-foreground">{t('chat:banner.backend_unavailable')}</span>
        ) : bannerErrorMessage ? (
          <Ellipsis className="text-muted-foreground">{bannerErrorMessage}</Ellipsis>
        ) : null}
      </div>
    </div>
  );
}
