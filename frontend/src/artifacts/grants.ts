import type { Viewer } from '../../artifact-runtime/types';
import type { Manifest } from './types';
export const SERVED_CAPABILITIES = [
  'artifact',
  'db',
  'sample',
  'agent',
  'user',
  'room',
  'downloads',
  'permissions',
] as const;
export function computeGrants(
  manifest: Manifest,
  viewer: Viewer,
  historical = false,
  pinned = false,
  assistant = false,
): string[] {
  if (historical || viewer.level === 'none') return [];
  return SERVED_CAPABILITIES.filter((name) => {
    if (name === 'permissions') return true;
    if (!Object.hasOwn(manifest, name) || manifest[name] === false)
      return false;
    if (name === 'sample')
      return !viewer.guest && (!assistant || viewer.level === 'owner');
    if (name === 'agent')
      return (
        !viewer.guest &&
        ['interact', 'owner'].includes(viewer.level) &&
        (!assistant || viewer.level === 'owner')
      );
    if (name === 'artifact') return !pinned;
    return true;
  });
}
