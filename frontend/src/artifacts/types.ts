import type { components } from '@/api/schema';
import type { Viewer } from '../../artifact-runtime/types';
export type ArtifactKind = components['schemas']['ArtifactRecord']['kind'];
export type Manifest = Partial<
  Record<
    'artifact' | 'db' | 'sample' | 'agent' | 'user' | 'room' | 'downloads',
    Record<string, unknown> | boolean
  >
>;
export interface ArtifactOrigin {
  session_kind: string;
  session_id?: string;
  agent_id?: string;
  assistant_id?: string;
}
export type ArtifactRecord = components['schemas']['ArtifactRecord'] & {
  manifest: Manifest;
  origin: ArtifactOrigin;
};
export interface ArtifactVersion {
  version: number;
  title: string;
  kind: ArtifactKind;
  content: string;
  language?: string | null;
  manifest: Manifest;
  created_at: string;
}
export type ArtifactVersionSummary = Omit<ArtifactVersion, 'content'>;
export type ArtifactRuntimeRecord = Omit<components['schemas']['ArtifactRuntimeRecord'], 'manifest' | 'origin'> & {
  manifest: Manifest;
  origin: Partial<ArtifactOrigin>;
};
export type ArtifactRuntime = Omit<components['schemas']['ArtifactRuntime'], 'artifact' | 'manifest' | 'viewer'> & {
  artifact: ArtifactRuntimeRecord;
  manifest: Manifest;
  viewer: Viewer;
};
export type ArtifactShare = components['schemas']['ArtifactShare'];
export type ArtifactPage = Omit<components['schemas']['ArtifactPage'], 'items'> & {
  items: ArtifactRecord[];
};
export type ArtifactTarget =
  | { id: string; token?: never }
  | { token: string; guestId: string; id?: never };
export interface ArtifactMarker {
  id: string;
  version: number;
  title: string;
  kind: ArtifactKind;
}
