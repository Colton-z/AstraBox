export const CHANNEL = 'astrabox-artifact';
export const CONTRACT = '0.2.67';
export const STORAGE_LIMIT = 5 * 1024 * 1024;
export type RuntimeError = { code: string; message: string; text?: string };
export type Viewer = {
  level: 'none' | 'view' | 'interact' | 'owner';
  id: string | null;
  guest: boolean;
  name?: string;
};
export type BridgeMessage = {
  channel: typeof CHANNEL;
  type: string;
  contract?: string;
  id?: string;
  capability?: string;
  method?: string;
  args?: unknown[];
  subscription?: string;
  payload?: unknown;
  granted?: string[];
  viewer?: Viewer;
  storage?: Record<string, string>;
  version?: number;
  ok?: boolean;
  value?: unknown;
  error?: RuntimeError;
  message?: string;
  stack?: string;
};
export type Row = { path: string; id?: string; data: Record<string, unknown> };
export type DocumentSnapshot = {
  exists: boolean;
  id: string;
  data: () => Record<string, unknown> | undefined;
};
export type DocumentChange = {
  type: 'added' | 'modified' | 'removed';
  doc: DocumentSnapshot;
  oldIndex: number;
  newIndex: number;
};
export function documentSnapshot(
  path: string,
  data?: Record<string, unknown> | null,
): DocumentSnapshot {
  return Object.freeze({
    exists: data != null,
    id: path.split('/').at(-1)!,
    data: () => (data == null ? undefined : structuredClone(data)),
  });
}
function orderedJSON(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(orderedJSON);
  if (value && typeof value === 'object')
    return Object.fromEntries(
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => [key, orderedJSON(item)]),
    );
  return value;
}
export function querySnapshot(rows: Row[], previous: Row[] = []) {
  const docs = rows.map((row) => documentSnapshot(row.path, row.data));
  const before = new Map(
    previous.map((row, index) => [row.path, { row, index }]),
  );
  const after = new Map(rows.map((row, index) => [row.path, index]));
  const changes: DocumentChange[] = [];
  previous.forEach((row, oldIndex) => {
    if (!after.has(row.path))
      changes.push({
        type: 'removed',
        doc: documentSnapshot(row.path, row.data),
        oldIndex,
        newIndex: -1,
      });
  });
  rows.forEach((row, newIndex) => {
    const old = before.get(row.path);
    if (!old)
      changes.push({
        type: 'added',
        doc: docs[newIndex],
        oldIndex: -1,
        newIndex,
      });
    else if (
      old.index !== newIndex ||
      JSON.stringify(orderedJSON(old.row.data)) !==
        JSON.stringify(orderedJSON(row.data))
    ) {
      changes.push({
        type: 'modified',
        doc: docs[newIndex],
        oldIndex: old.index,
        newIndex,
      });
    }
  });
  return Object.freeze({
    docs,
    size: docs.length,
    empty: !docs.length,
    docChanges: () => changes,
  });
}
export function storageBytes(values: Record<string, string>) {
  return Object.entries(values).reduce(
    (size, [key, value]) => size + 2 * (key.length + value.length),
    0,
  );
}
