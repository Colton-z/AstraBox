// @vitest-environment jsdom
/**
 * The tree rows the panel composes itself, at the level a browser is not
 * needed for: which controls a directory row carries, what disclosing one does,
 * and the classes that keep a long name inside the panel.
 *
 * `tests/e2e-ui/specs/file-panel-completes-native-filesystem-journey.exclusive.spec.ts`
 * drives the same rows against a real sandbox — mkdir, upload, rename, delete —
 * and `tests/e2e-ui/specs/visual-grammar.parallel.spec.ts` is where the width
 * discipline is measured as layout rather than as class names.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';

const ROOT = '/home/user/workspace';
const DIRECTORY = `${ROOT}/docs`;
const LONG_NAME = 'a-very-long-file-name-that-has-to-truncate-rather-than-widen-the-panel.bin';

const ensureDirectoryLoaded = vi.fn(async () => {});

vi.mock('../hooks/useSessionFiles', () => ({
  useSessionFiles: () => ({
    rootPath: ROOT,
    directoryEntriesByPath: {
      [ROOT]: [
        { path: DIRECTORY, name: 'docs', kind: 'directory', size: null },
        { path: `${ROOT}/${LONG_NAME}`, name: LONG_NAME, kind: 'file', size: 2048 },
      ],
      [DIRECTORY]: [],
    },
    directoryLoadingPaths: new Set<string>(),
    loading: false,
    mutating: false,
    loadedOnce: true,
    error: null,
    ensureDirectoryLoaded,
    refresh: async () => {},
    upload: async () => {},
    mkdir: async () => {},
    rename: async () => {},
    delete: async () => {},
    download: async () => {},
  }),
}));

const { default: SessionFilesPanel } = await import('./SessionFilesPanel');

function directoryRow(name: string): HTMLElement {
  const row = screen.getByText(name, { exact: true }).closest('[role="treeitem"]');
  if (!row) throw new Error(`no treeitem around ${name}`);
  return row as HTMLElement;
}

afterEach(cleanup);
beforeEach(() => {
  ensureDirectoryLoaded.mockClear();
});
beforeAll(async () => {
  await i18n.changeLanguage('en');
});

describe('SessionFilesPanel', () => {
  it('gives every directory row its own actions, and says which one is the root', () => {
    render(<SessionFilesPanel sessionId="session-1" />);

    // The root row is the one place the panel names the workspace directory as
    // such; a tree of identical rows leaves the reader guessing where it starts.
    expect(screen.getAllByText(i18n.t('chat:files.root_directory')).length).toBeGreaterThan(0);

    // Rename and delete are reached per row, so a directory below the root has
    // to carry them too — otherwise only files can be renamed or deleted.
    const row = directoryRow('docs');
    expect(row.querySelector(`[title="${i18n.t('chat:files.more_actions')}"]`)).not.toBeNull();
  });

  it('loads a directory when it is disclosed, and does not disclose it when it is selected', () => {
    render(<SessionFilesPanel sessionId="session-1" />);
    const row = directoryRow('docs');
    const [disclose, select] = [...row.querySelectorAll('button')];

    // Selecting a directory aims the toolbar's upload and new-folder at it.
    // That is a different act from looking inside, so it must not open the row.
    fireEvent.click(select);
    expect(disclose.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(disclose);
    expect(disclose.getAttribute('aria-expanded')).toBe('true');
    // Contents are fetched on disclosure: an open directory that never asked
    // for its entries renders as permanently empty.
    expect(ensureDirectoryLoaded).toHaveBeenCalledWith(DIRECTORY, { force: false });
  });

  it('holds a long name inside the panel instead of widening it', () => {
    render(<SessionFilesPanel sessionId="session-1" />);

    // jsdom does no layout, so this asserts the mechanism rather than the
    // result: a flex child only truncates while its ancestors allow it to
    // shrink, which is what `min-w-0` on each row says.
    for (const item of document.querySelectorAll('[role="treeitem"]')) {
      expect(item.className, item.textContent?.slice(0, 24)).toContain('min-w-0');
    }
    expect(screen.getByText(LONG_NAME).className).toContain('truncate');
  });
});
