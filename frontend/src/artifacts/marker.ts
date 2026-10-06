import type { ArtifactMarker } from './types';
const kinds = new Set(['html', 'react', 'svg', 'markdown', 'mermaid', 'code']);
/** Find markers within 12 levels, 4096 values and 8,388,608 JSON text characters. */
export function parseArtifactMarkers(output: unknown): ArtifactMarker[] {
  const result = new Map<string, ArtifactMarker>();
  const visited = new Set<object>();
  let remainingValues = 4096;
  let remainingText = 8 * 1024 * 1024;
  const read = (value: unknown, depth: number) => {
    if (depth > 12 || remainingValues <= 0) return;
    remainingValues--;
    if (typeof value === 'string') {
      if (value.length > remainingText) return;
      remainingText -= value.length;
      if (!value.includes('astrabox_artifact')) return;
      try {
        const parsed: unknown = JSON.parse(value);
        if (parsed !== null && typeof parsed === 'object') read(parsed, depth + 1);
      } catch {
        /* Invalid JSON is not a marker. */
      }
      return;
    }
    if (value === null || typeof value !== 'object' || visited.has(value)) return;
    visited.add(value);
    if (Array.isArray(value)) {
      for (const item of value) {
        if (remainingValues <= 0) break;
        read(item, depth + 1);
      }
    } else {
      const record = value as Record<string, unknown>;
      const marker = record.astrabox_artifact as Partial<ArtifactMarker> | undefined;
      if (
        marker &&
        typeof marker.id === 'string' &&
        /^art_[0-9a-f]{24}$/.test(marker.id) &&
        Number.isInteger(marker.version) &&
        marker.version! > 0 &&
        typeof marker.title === 'string' &&
        typeof marker.kind === 'string' &&
        kinds.has(marker.kind)
      ) {
        result.set(`${marker.id}:${marker.version}`, {
          id: marker.id, version: marker.version!, title: marker.title, kind: marker.kind,
        });
      }
      for (const key in record) {
        if (remainingValues <= 0) break;
        if (Object.hasOwn(record, key)) read(record[key], depth + 1);
      }
    }
  };
  read(output, 0);
  return [...result.values()];
}
