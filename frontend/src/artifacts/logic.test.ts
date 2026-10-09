import { describe, expect, it } from 'vitest';
import { cardArtifacts } from './marker';
import { computeGrants } from './grants';
import { querySnapshot } from '../../artifact-runtime/types';
const marker = {
  id: `art_${'a'.repeat(24)}`,
  version: 2,
  title: 'Dashboard',
  kind: 'html',
};
describe('artifact cards', () => {
  const card = (artifacts: unknown, kind = 'artifact') => ({
    type: 'data-tool-card',
    data: { toolCallId: 'call', cardId: 'crd_1', kind, artifacts },
  });
  it('reads the artifacts a card part shows', () => {
    expect(cardArtifacts(card([marker]))).toEqual([marker]);
  });
  it('reads nothing from another part, another card kind or a malformed entry', () => {
    expect(cardArtifacts({ type: 'dynamic-tool', data: { kind: 'artifact', artifacts: [marker] } })).toEqual([]);
    expect(cardArtifacts(card([marker], 'connection_request'))).toEqual([]);
    expect(cardArtifacts(card([{ ...marker, id: 'wrong' }, { ...marker, kind: 'pdf' }, marker]))).toEqual([marker]);
  });
});
describe('capability grants', () => {
  const manifest = {
    artifact: {},
    db: {},
    sample: {},
    agent: {},
    user: {},
    room: {},
    downloads: true,
  };
  it('intersects declarations, viewer level and served names', () => {
    expect(
      computeGrants(manifest, { level: 'view', id: null, guest: true }),
    ).toEqual(['artifact', 'db', 'user', 'room', 'downloads', 'permissions']);
    expect(
      computeGrants({ user: {}, files: {} } as any, {
        level: 'owner',
        id: 'owner',
        guest: false,
      }),
    ).toEqual(['user', 'permissions']);
  });
  it('removes historical capabilities and publishing on pinned shares', () => {
    const owner = { level: 'owner', id: 'owner', guest: false } as const;
    expect(computeGrants(manifest, owner, true)).toEqual([]);
    expect(computeGrants(manifest, owner, false, true)).not.toContain(
      'artifact',
    );
  });
  it('restricts Assistant model and Agent work to the owner', () => {
    expect(
      computeGrants(
        manifest,
        { level: 'interact', id: 'other', guest: false },
        false,
        false,
        true,
      ),
    ).not.toContain('sample');
    expect(
      computeGrants(
        manifest,
        { level: 'interact', id: 'other', guest: false },
        false,
        false,
        true,
      ),
    ).not.toContain('agent');
  });
});
it('diffs query snapshots across additions, updates, moves and removals', () => {
  const previous = [
    { path: 'items/a', data: { n: 1 } },
    { path: 'items/b', data: { n: 2 } },
  ];
  const next = [
    { path: 'items/b', data: { n: 3 } },
    { path: 'items/c', data: { n: 4 } },
  ];
  const snapshot = querySnapshot(next, previous);
  expect(snapshot.size).toBe(2);
  expect(snapshot.empty).toBe(false);
  expect(snapshot.docs[0].exists).toBe(true);
  expect(
    snapshot
      .docChanges()
      .map((change) => [
        change.type,
        change.doc.id,
        change.oldIndex,
        change.newIndex,
      ]),
  ).toEqual([
    ['removed', 'a', 0, -1],
    ['modified', 'b', 1, 0],
    ['added', 'c', -1, 1],
  ]);
  expect(querySnapshot(next, next).docChanges()).toEqual([]);
  expect(querySnapshot([]).empty).toBe(true);
  const data = snapshot.docs[0].data()!;
  data.n = 99;
  expect(snapshot.docs[0].data()).toEqual({ n: 3 });
});

it('does not treat JSON object key order as a document edit', () => {
  expect(
    querySnapshot(
      [{ path: 'items/a', data: { a: 1, b: { x: 2, y: 3 } } }],
      [{ path: 'items/a', data: { b: { y: 3, x: 2 }, a: 1 } }],
    ).docChanges(),
  ).toEqual([]);
});
