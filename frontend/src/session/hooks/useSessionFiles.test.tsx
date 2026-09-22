// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { SessionFilesListResponse } from '../../types';

const api = vi.hoisted(() => ({
  buildSessionFileDownloadUrl: vi.fn(),
  createSessionDirectory: vi.fn(),
  deleteSessionFiles: vi.fn(),
  listSessionFiles: vi.fn(),
  moveSessionFiles: vi.fn(),
  uploadSessionFiles: vi.fn(),
}));

vi.mock('../../api', () => api);

import { useSessionFiles } from './useSessionFiles';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const rootPath = '/workspace';
const folderPath = `${rootPath}/reports`;
const rootWithFolder: SessionFilesListResponse = {
  root_path: rootPath,
  current_path: rootPath,
  parent_path: null,
  entries: [{
    path: folderPath,
    name: 'reports',
    kind: 'directory',
  }],
};
const emptyRoot: SessionFilesListResponse = {
  ...rootWithFolder,
  entries: [],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

async function renderLoadedFileTree() {
  api.listSessionFiles.mockResolvedValueOnce(rootWithFolder);
  const hook = renderHook(() => useSessionFiles('session-1'));
  await waitFor(() => expect(hook.result.current.loadedOnce).toBe(true));
  expect(hook.result.current.directoryEntriesByPath[rootPath]).toEqual(
    rootWithFolder.entries,
  );
  return hook;
}

describe('useSessionFiles mutation authority', () => {
  it('removes a confirmed deletion without waiting for the background listing', async () => {
    const refreshListing = deferred<SessionFilesListResponse>();
    const { result } = await renderLoadedFileTree();
    api.deleteSessionFiles.mockResolvedValue({ paths: [folderPath], deleted_count: 1 });
    api.listSessionFiles.mockReturnValueOnce(refreshListing.promise);

    await act(async () => {
      await result.current.delete([folderPath]);
    });

    expect(result.current.directoryEntriesByPath[rootPath]).toEqual([]);
    expect(result.current.directoryEntriesByPath[folderPath]).toBeUndefined();

    await act(async () => {
      refreshListing.resolve(emptyRoot);
      await refreshListing.promise;
    });
  });

  it('does not let a listing started before deletion restore the deleted path', async () => {
    const staleListing = deferred<SessionFilesListResponse>();
    const { result } = await renderLoadedFileTree();
    api.listSessionFiles
      .mockReturnValueOnce(staleListing.promise)
      .mockResolvedValueOnce(emptyRoot);
    api.deleteSessionFiles.mockResolvedValue({ paths: [folderPath], deleted_count: 1 });

    let staleRefresh!: Promise<SessionFilesListResponse[]>;
    act(() => {
      staleRefresh = result.current.refresh({ paths: [rootPath], force: true });
    });
    await waitFor(() => expect(api.listSessionFiles).toHaveBeenCalledTimes(2));

    await act(async () => {
      await result.current.delete([folderPath]);
    });
    await waitFor(() => expect(api.listSessionFiles).toHaveBeenCalledTimes(3));
    expect(result.current.directoryEntriesByPath[rootPath]).toEqual([]);

    await act(async () => {
      staleListing.resolve(rootWithFolder);
      await staleRefresh;
    });

    expect(result.current.directoryEntriesByPath[rootPath]).toEqual([]);
  });
});
