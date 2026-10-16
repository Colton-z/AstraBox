import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { FileText } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useArtifactNavigation } from './navigation';
import type { ArtifactMarker } from './types';
export function ArtifactToolCard({
  marker,
}: {
  marker: ArtifactMarker;
}) {
  const { t } = useTranslation();
  const navigation = useArtifactNavigation();
  return (
    <div
      className="flex items-center gap-3 rounded-md border border-border bg-card p-3"
      data-testid="artifact-tool-card"
    >
      <FileText className="size-5 shrink-0 text-muted-foreground" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="truncate text-sm font-medium">{marker.title}</span>
        <span className="text-xs text-muted-foreground">
          {t(`misc:artifacts.kind_${marker.kind}`)} ·{' '}
          {t('misc:artifacts.version', { version: marker.version })}
        </span>
      </div>
      {navigation.open ? (
        <Button
          size="sm"
          variant="outline"
          onClick={() => navigation.open!(marker.id)}
        >
          {t('misc:artifacts.open')}
        </Button>
      ) : (
        !navigation.sharedTranscript && (
          <Button
            size="sm"
            variant="outline"
            render={<Link to={`/artifacts/${marker.id}`} />}
          >
            {t('misc:artifacts.open')}
          </Button>
        )
      )}
    </div>
  );
}
