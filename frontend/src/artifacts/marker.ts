import type { ArtifactMarker } from './types';
const kinds = new Set(['html', 'react', 'svg', 'markdown', 'mermaid', 'code']);
/**
 * The artifacts a platform card part shows.
 *
 * The server adds a `data-tool-card` part only for a card its artifact tool
 * recorded in this Session, so the console never reads a tool's own output to
 * decide what to draw.
 */
export function cardArtifacts(part: { type: string; data?: unknown }): ArtifactMarker[] {
  if (part.type !== 'data-tool-card') return [];
  const data = part.data as { kind?: unknown; artifacts?: unknown } | undefined;
  if (data?.kind !== 'artifact' || !Array.isArray(data.artifacts)) return [];
  return data.artifacts.flatMap((value) => {
    const marker = value as Partial<ArtifactMarker> | null;
    return marker
      && typeof marker.id === 'string'
      && /^art_[0-9a-f]{24}$/.test(marker.id)
      && Number.isInteger(marker.version)
      && marker.version! > 0
      && typeof marker.title === 'string'
      && typeof marker.kind === 'string'
      && kinds.has(marker.kind)
      ? [{ id: marker.id, version: marker.version!, title: marker.title, kind: marker.kind }]
      : [];
  });
}
