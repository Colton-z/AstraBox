// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { ArtifactFrame } from './ArtifactFrame';
vi.mock('./BridgeHost', () => ({
  ArtifactPageError: class extends Error {},
  BridgeHost: class {
    stop() {}
  },
}));
vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock('next-themes', () => ({
  useTheme: () => ({ resolvedTheme: 'light' }),
}));
it('renders an opaque iframe that cannot download directly', () => {
  const { container } = render(
    <ArtifactFrame
      kind="html"
      target={{ id: `art_${'a'.repeat(24)}` }}
      onVersion={() => {}}
      runtime={{
        artifact: {
          artifact_id: `art_${'a'.repeat(24)}`,
          title: 'Page',
          description: '',
          kind: 'html',
          current_version: 1,
          manifest: {},
          origin: { session_kind: 'agent_chat' },
          created_at: '',
          updated_at: '',
        },
        version: 1,
        content: '<p>Page</p>',
        manifest: {},
        viewer: { level: 'owner', id: 'owner', guest: false },
        pinned: false,
        after_seq: 0,
      }}
    />,
  );
  const frame = container.querySelector('iframe')!;
  expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin');
  expect(frame.getAttribute('sandbox')).not.toContain('allow-downloads');
  expect(frame.srcdoc).toContain('Content-Security-Policy');
});
