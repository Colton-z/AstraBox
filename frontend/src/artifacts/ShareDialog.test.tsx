// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SWRConfig } from 'swr';

import i18n from '../i18n';
import { artifactsApi } from './api';
import { ArtifactViewer } from './ArtifactViewer';
import type { ArtifactRuntime, ArtifactShare, ArtifactVersion, Manifest } from './types';

vi.mock('./ArtifactFrame', () => ({
  ArtifactFrame: ({ runtime }: { runtime: ArtifactRuntime }) => (
    <div data-testid="artifact-frame" data-version={runtime.version}>{runtime.content}</div>
  ),
}));
vi.mock('@/session/components/MessageParts', () => ({ MarkdownContent: () => null }));
vi.mock('@/components/ai-elements/code-block', () => ({ CodeBlock: () => null }));

beforeAll(async () => { await i18n.changeLanguage('en'); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const id = `art_${'a'.repeat(24)}`;
const share: ArtifactShare = {
  enabled: false, token: '', access: 'view', audience: 'signed_in', version: null,
  created_at: null, expires_at: null,
};
function renderViewer(fullPage: boolean, currentManifest: Manifest, metadataManifest: Manifest = {}, historicalManifest: Manifest = {}) {
  const runtime: ArtifactRuntime = {
    artifact: {
      artifact_id: id, title: 'Page', description: '', kind: 'html', current_version: 2,
      manifest: metadataManifest, origin: { session_kind: 'agent_chat' }, created_at: '', updated_at: '',
    },
    version: 2, content: 'Current page', manifest: currentManifest,
    viewer: { level: 'owner', id: 'owner', guest: false }, pinned: false, after_seq: 0,
  };
  const historical: ArtifactVersion = {
    version: 1, title: 'Old page', kind: 'html', content: 'Historical page',
    manifest: historicalManifest, created_at: '',
  };
  vi.spyOn(artifactsApi, 'runtime').mockResolvedValue(runtime);
  vi.spyOn(artifactsApi, 'versions').mockResolvedValue([
    historical, { ...historical, version: 2, title: 'Page', manifest: currentManifest },
  ]);
  vi.spyOn(artifactsApi, 'version').mockResolvedValue(historical);
  vi.spyOn(artifactsApi, 'share').mockResolvedValue(share);
  const createShare = vi.spyOn(artifactsApi, 'createShare').mockResolvedValue(share);
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MemoryRouter><ArtifactViewer target={{ id }} fullPage={fullPage} /></MemoryRouter>
    </SWRConfig>,
  );
  return createShare;
}

const scenes = [
  { surface: 'panel', fullPage: false, capability: 'sample' },
  { surface: 'full page', fullPage: true, capability: 'sample' },
  { surface: 'panel', fullPage: false, capability: 'agent' },
  { surface: 'full page', fullPage: true, capability: 'agent' },
];
describe('sharing through the artifact viewer', () => {
  it.each(scenes)('uses current $capability policy in the $surface share dialog', async ({ fullPage, capability }) => {
    const createShare = renderViewer(fullPage, { [capability]: {} });
    fireEvent.click(await screen.findByRole('button', { name: 'Share artifact' }));
    const audience = await screen.findByRole('combobox', { name: 'Audience' });
    await waitFor(() => expect((audience as HTMLSelectElement).value).toBe('signed_in'));
    expect(audience.querySelector<HTMLOptionElement>('option[value="anyone"]')!.disabled).toBe(true);
    expect(screen.getByText('This artifact calls a model or runs an Agent. Viewers must sign in.')).toBeTruthy();
    const save = screen.getByRole('button', { name: 'Save' });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(createShare).toHaveBeenCalledWith(id, {
      access: 'view', audience: 'signed_in', version: null,
    }));
  });

  it.each(scenes)('retains current $capability policy while the $surface views history', async ({ fullPage, capability }) => {
    renderViewer(fullPage, { [capability]: {} });
    const versions = await screen.findByRole('combobox', { name: 'Version history' });
    fireEvent.change(versions, { target: { value: '1' } });
    await screen.findByText('Historical page');
    fireEvent.click(screen.getByRole('button', { name: 'Share artifact' }));
    const audience = await screen.findByRole('combobox', { name: 'Audience' });
    await waitFor(() => expect((audience as HTMLSelectElement).value).toBe('signed_in'));
    expect(audience.querySelector<HTMLOptionElement>('option[value="anyone"]')!.disabled).toBe(true);
  });

  it('allows an anonymous audience when model capability exists only in historical metadata', async () => {
    renderViewer(true, {}, { sample: {} }, { sample: {} });
    const versions = await screen.findByRole('combobox', { name: 'Version history' });
    fireEvent.change(versions, { target: { value: '1' } });
    await screen.findByText('Historical page');
    fireEvent.click(screen.getByRole('button', { name: 'Share artifact' }));
    const audience = await screen.findByRole('combobox', { name: 'Audience' });
    expect(audience.querySelector<HTMLOptionElement>('option[value="anyone"]')!.disabled).toBe(false);
    fireEvent.change(audience, { target: { value: 'anyone' } });
    expect((audience as HTMLSelectElement).value).toBe('anyone');
  });
});
