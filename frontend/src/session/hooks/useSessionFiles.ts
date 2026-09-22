import { useCallback, useEffect, useRef, useState } from 'react';
import i18n from '../../i18n';
import { keepsLastRead } from '../../hooks/useKeepCurrent';
import {
  ApiError,
  buildSessionFileDownloadUrl,
  createSessionDirectory,
  deleteSessionFiles,
  listSessionFiles,
  moveSessionFiles,
  uploadSessionFiles,
} from '../../api';
import type {
  SessionFileEntry,
  SessionFilesListResponse,
} from '../../types';

type RefreshSessionFilesOptions = {
  force?: boolean;
  paths?: string[];
  background?: boolean;
};

type UploadSessionFilesOptions = {
  path?: string;
};

export type SessionFilesDirectoryEntries = Record<string, SessionFileEntry[]>;

export type UseSessionFilesResult = {
  rootPath: string | null;
  directoryEntriesByPath: SessionFilesDirectoryEntries;
  directoryLoadingPaths: ReadonlySet<string>;
  loading: boolean;
  mutating: boolean;
  loadedOnce: boolean;
  error: string | null;
  ensureDirectoryLoaded: (
    path: string,
    options?: { force?: boolean },
  ) => Promise<SessionFilesListResponse>;
  refresh: (options?: RefreshSessionFilesOptions) => Promise<SessionFilesListResponse[]>;
  upload: (files: File[] | FileList, options?: UploadSessionFilesOptions) => Promise<SessionFilesListResponse[]>;
  mkdir: (path: string) => Promise<SessionFilesListResponse[]>;
  move: (srcPath: string, destPath: string) => Promise<SessionFilesListResponse[]>;
  rename: (srcPath: string, destPath: string) => Promise<SessionFilesListResponse[]>;
  delete: (paths: string[]) => Promise<SessionFilesListResponse[]>;
  download: (path: string) => Promise<void>;
  getDownloadUrl: (path: string) => string;
};

type UseSessionFilesOptions = {
  enabled?: boolean;
};

function getErrorMessage(error: unknown): string {
  // A refusal the platform has an answer for is not news for the reader: the
  // bound sandbox being gone starts a replacement. Its wire text is addressed
  // to an operator — a code, a provider name, a sandbox identifier — and a
  // person reading a file list can act on none of it. Branch on the code,
  // which is the machine-readable half.
  if (error instanceof ApiError && error.code === 'SANDBOX_GONE') {
    return i18n.t('chat:files.unavailable_sandbox_gone');
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error ?? 'Unknown error');
}

function normalizePath(path: string): string {
  if (path === '/') {
    return '/';
  }
  return path.replace(/\/+$/, '');
}

function getParentPath(path: string): string | null {
  const normalizedPath = normalizePath(path);
  if (!normalizedPath || normalizedPath === '/') {
    return null;
  }

  const lastSlashIndex = normalizedPath.lastIndexOf('/');
  if (lastSlashIndex <= 0) {
    return '/';
  }
  return normalizedPath.slice(0, lastSlashIndex);
}

function toFileArray(files: File[] | FileList): File[] {
  return Array.isArray(files) ? files : Array.from(files);
}

function inferDownloadName(path: string): string {
  const normalized = normalizePath(String(path || '').trim());
  if (!normalized || normalized === '/') {
    return 'download';
  }
  const segments = normalized.split('/');
  return segments[segments.length - 1] || 'download';
}

function uniquePaths(paths: Array<string | null | undefined>): string[] {
  return Array.from(
    new Set(
      paths
        .map((path) => String(path || '').trim())
        .filter(Boolean)
        .map((path) => normalizePath(path)),
    ),
  );
}

function isSameOrDescendantPath(path: string, ancestorPath: string): boolean {
  const normalizedPath = normalizePath(path);
  const normalizedAncestorPath = normalizePath(ancestorPath);
  return normalizedPath === normalizedAncestorPath
    || normalizedPath.startsWith(`${normalizedAncestorPath}/`);
}

function triggerBrowserDownload(url: string, filename: string): void {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function buildCachedListing(
  rootPath: string,
  path: string,
  directoryEntriesByPath: SessionFilesDirectoryEntries,
): SessionFilesListResponse {
  return {
    root_path: rootPath,
    current_path: path,
    parent_path: path === rootPath ? null : getParentPath(path),
    entries: directoryEntriesByPath[path] ?? [],
  };
}

export function useSessionFiles(
  sessionId: string,
  options: UseSessionFilesOptions = {},
): UseSessionFilesResult {
  const enabled = options.enabled !== false;
  const [rootPath, setRootPath] = useState<string | null>(null);
  const [directoryEntriesByPath, setDirectoryEntriesByPath] = useState<SessionFilesDirectoryEntries>({});
  const [directoryLoadingPaths, setDirectoryLoadingPaths] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(enabled);
  const [mutating, setMutating] = useState(false);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const rootPathRef = useRef<string | null>(null);
  const directoryEntriesByPathRef = useRef<SessionFilesDirectoryEntries>({});
  const loadedDirectoryPathsRef = useRef<Set<string>>(new Set());
  const sessionGenerationRef = useRef(0);
  const fileTreeRevisionRef = useRef(0);

  useEffect(() => {
    rootPathRef.current = rootPath;
  }, [rootPath]);

  useEffect(() => {
    directoryEntriesByPathRef.current = directoryEntriesByPath;
  }, [directoryEntriesByPath]);

  useEffect(() => {
    sessionGenerationRef.current += 1;
    fileTreeRevisionRef.current += 1;
    setRootPath(null);
    setDirectoryEntriesByPath({});
    setDirectoryLoadingPaths(new Set());
    setLoading(enabled);
    setMutating(false);
    setLoadedOnce(false);
    setError(null);
    rootPathRef.current = null;
    directoryEntriesByPathRef.current = {};
    loadedDirectoryPathsRef.current = new Set();
  }, [sessionId]);

  const updateDirectoryLoading = useCallback((path: string, isLoading: boolean) => {
    setDirectoryLoadingPaths((current) => {
      const next = new Set(current);
      if (isLoading) {
        next.add(path);
      } else {
        next.delete(path);
      }
      return next;
    });
  }, []);

  const applyListing = useCallback((listing: SessionFilesListResponse) => {
    setRootPath(listing.root_path);
    setDirectoryEntriesByPath((current) => ({
      ...current,
      [listing.current_path]: listing.entries,
    }));
    setLoadedOnce(true);
    rootPathRef.current = listing.root_path;
    directoryEntriesByPathRef.current = {
      ...directoryEntriesByPathRef.current,
      [listing.current_path]: listing.entries,
    };
    loadedDirectoryPathsRef.current = new Set(loadedDirectoryPathsRef.current).add(listing.current_path);
  }, []);

  const pruneLoadedDirectories = useCallback((paths: string[]) => {
    const targets = uniquePaths(paths);
    if (targets.length === 0) {
      return;
    }

    setDirectoryEntriesByPath((current) => {
      const nextEntries: SessionFilesDirectoryEntries = {};
      for (const [path, entries] of Object.entries(current)) {
        if (targets.some((target) => isSameOrDescendantPath(path, target))) {
          continue;
        }
        nextEntries[path] = entries;
      }
      directoryEntriesByPathRef.current = nextEntries;
      return nextEntries;
    });

    loadedDirectoryPathsRef.current = new Set(
      Array.from(loadedDirectoryPathsRef.current).filter(
        (path) => !targets.some((target) => isSameOrDescendantPath(path, target)),
      ),
    );
  }, []);

  const removeDeletedPathsFromFileTree = useCallback((paths: string[]) => {
    const targets = uniquePaths(paths);
    if (targets.length === 0) {
      return;
    }

    setDirectoryEntriesByPath((current) => {
      const nextEntries: SessionFilesDirectoryEntries = {};
      for (const [path, entries] of Object.entries(current)) {
        if (targets.some((target) => isSameOrDescendantPath(path, target))) {
          continue;
        }
        nextEntries[path] = entries.filter(
          (entry) => !targets.some((target) => isSameOrDescendantPath(entry.path, target)),
        );
      }
      directoryEntriesByPathRef.current = nextEntries;
      return nextEntries;
    });

    loadedDirectoryPathsRef.current = new Set(
      Array.from(loadedDirectoryPathsRef.current).filter(
        (path) => !targets.some((target) => isSameOrDescendantPath(path, target)),
      ),
    );
  }, []);

  const clearFileTreeState = useCallback(() => {
    setRootPath(null);
    setDirectoryEntriesByPath({});
    setDirectoryLoadingPaths(new Set());
    setLoadedOnce(false);
    rootPathRef.current = null;
    directoryEntriesByPathRef.current = {};
    loadedDirectoryPathsRef.current = new Set();
  }, []);

  const assertEnabled = useCallback(() => {
    if (enabled) {
      return;
    }
    const nextError = 'SESSION_FILES_DISABLED: file panel is disabled.';
    setError(nextError);
    throw new Error(nextError);
  }, [enabled]);

  const loadDirectory = useCallback(async (
    path?: string,
    options: { force?: boolean; background?: boolean } = {},
  ): Promise<SessionFilesListResponse> => {
    if (!enabled) {
      const resolvedPath = path ? normalizePath(path) : rootPathRef.current;
      if (!resolvedPath || !rootPathRef.current) {
        throw new Error('SESSION_FILES_DISABLED: file panel is disabled.');
      }
      return buildCachedListing(
        rootPathRef.current,
        resolvedPath,
        directoryEntriesByPathRef.current,
      );
    }

    const targetPath = path ? normalizePath(path) : undefined;
    if (targetPath && !options.force && directoryEntriesByPathRef.current[targetPath]) {
      const knownRootPath = rootPathRef.current;
      if (!knownRootPath) {
        const nextError = 'SESSION_FILES_ROOT_UNAVAILABLE: root path is not loaded yet.';
        setError(nextError);
        throw new Error(nextError);
      }
      return buildCachedListing(
        knownRootPath,
        targetPath,
        directoryEntriesByPathRef.current,
      );
    }

    const sessionGeneration = sessionGenerationRef.current;
    const fileTreeRevision = fileTreeRevisionRef.current;
    if (targetPath) {
      updateDirectoryLoading(targetPath, true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      const listing = await listSessionFiles(
        sessionId,
        targetPath === undefined ? {} : { path: targetPath },
      );

      if (
        sessionGeneration !== sessionGenerationRef.current
        || fileTreeRevision !== fileTreeRevisionRef.current
      ) {
        const knownRootPath = rootPathRef.current ?? listing.root_path;
        return buildCachedListing(
          knownRootPath,
          listing.current_path,
          directoryEntriesByPathRef.current,
        );
      }

      applyListing(listing);
      return listing;
    } catch (err) {
      if (
        sessionGeneration === sessionGenerationRef.current
        && fileTreeRevision === fileTreeRevisionRef.current
      ) {
        const hasListing = targetPath
          ? loadedDirectoryPathsRef.current.has(targetPath)
          : rootPathRef.current !== null;
        if (!keepsLastRead(err, { background: options.background === true }, hasListing)) {
          setError(getErrorMessage(err));
        }
      }
      throw err;
    } finally {
      if (targetPath) {
        updateDirectoryLoading(targetPath, false);
      } else if (sessionGeneration === sessionGenerationRef.current) {
        setLoading(false);
      }
    }
  }, [applyListing, enabled, sessionId, updateDirectoryLoading]);

  useEffect(() => {
    if (!enabled) {
      sessionGenerationRef.current += 1;
      clearFileTreeState();
      setLoading(false);
      setMutating(false);
      setError(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        await loadDirectory(undefined, { force: true });
      } catch {
        if (cancelled) {
          return;
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [clearFileTreeState, enabled, loadDirectory]);

  const refresh = useCallback(async (
    options: RefreshSessionFilesOptions = {},
  ): Promise<SessionFilesListResponse[]> => {
    if (!enabled) {
      if (!rootPathRef.current) {
        return [];
      }
      const explicitPaths = uniquePaths(options.paths ?? []);
      const refreshTargets = explicitPaths.length > 0
        ? explicitPaths
        : Array.from(loadedDirectoryPathsRef.current);
      if (refreshTargets.length === 0) {
        return [buildCachedListing(rootPathRef.current, rootPathRef.current, directoryEntriesByPathRef.current)];
      }
      return refreshTargets.map((path) => buildCachedListing(rootPathRef.current!, path, directoryEntriesByPathRef.current));
    }

    const explicitPaths = uniquePaths(options.paths ?? []);
    const refreshTargets = explicitPaths.length > 0
      ? explicitPaths
      : Array.from(loadedDirectoryPathsRef.current);

    if (refreshTargets.length === 0) {
      return [await loadDirectory(undefined, { force: true, background: options.background })];
    }

    return Promise.all(
      refreshTargets.map((path) => loadDirectory(path, { force: true, background: options.background })),
    );
  }, [enabled, loadDirectory]);

  useEffect(() => {
    if (!enabled) return;
    const reconnect = () => { void refresh({ background: true }).catch(() => {}); };
    window.addEventListener('online', reconnect);
    return () => window.removeEventListener('online', reconnect);
  }, [enabled, refresh]);

  const ensureDirectoryLoaded = useCallback(async (
    path: string,
    options: { force?: boolean } = {},
  ): Promise<SessionFilesListResponse> => {
    return loadDirectory(path, options);
  }, [loadDirectory]);

  const resolveTargetDirectoryPath = useCallback((path?: string): string => {
    if (path) {
      return normalizePath(path);
    }
    if (!rootPathRef.current) {
      const nextError = 'SESSION_FILES_ROOT_UNAVAILABLE: root path is not loaded yet.';
      setError(nextError);
      throw new Error(nextError);
    }
    return rootPathRef.current;
  }, []);

  const runMutation = useCallback(async (
    operation: () => Promise<unknown>,
    refreshPaths: string[],
    options: { afterSuccess?: () => void } = {},
  ): Promise<SessionFilesListResponse[]> => {
    assertEnabled();
    setMutating(true);
    setError(null);
    try {
      await operation();
      fileTreeRevisionRef.current += 1;
      options.afterSuccess?.();

      void refresh({ paths: refreshPaths, force: true }).catch((refreshError) => {
        const message = `SESSION_FILES_REFRESH_FAILED: file operation succeeded but refreshing the directory failed. ${getErrorMessage(refreshError)}`;
        setError(message);
      });
      return [];
    } catch (err) {
      setError(getErrorMessage(err));
      throw err;
    } finally {
      setMutating(false);
    }
  }, [assertEnabled, refresh]);

  const upload = useCallback(async (
    files: File[] | FileList,
    options: UploadSessionFilesOptions = {},
  ): Promise<SessionFilesListResponse[]> => {
    const nextFiles = toFileArray(files);
    if (nextFiles.length === 0) {
      const nextError = 'SESSION_FILES_UPLOAD_EMPTY: at least one file is required.';
      setError(nextError);
      throw new Error(nextError);
    }

    const targetPath = resolveTargetDirectoryPath(options.path);
    return runMutation(
      () => uploadSessionFiles(sessionId, { path: targetPath, files: nextFiles }),
      [targetPath],
    );
  }, [resolveTargetDirectoryPath, runMutation, sessionId]);

  const mkdir = useCallback(async (path: string): Promise<SessionFilesListResponse[]> => {
    const targetPath = normalizePath(path);
    const refreshPaths = uniquePaths([getParentPath(targetPath), rootPathRef.current]);
    return runMutation(
      () => createSessionDirectory(sessionId, { path: targetPath }),
      refreshPaths,
    );
  }, [runMutation, sessionId]);

  const move = useCallback(async (
    srcPath: string,
    destPath: string,
  ): Promise<SessionFilesListResponse[]> => {
    const normalizedSrcPath = normalizePath(srcPath);
    const normalizedDestPath = normalizePath(destPath);
    const refreshPaths = uniquePaths([
      getParentPath(normalizedSrcPath),
      getParentPath(normalizedDestPath),
      rootPathRef.current,
    ]);
    return runMutation(
      () => moveSessionFiles(sessionId, {
        src_path: normalizedSrcPath,
        dest_path: normalizedDestPath,
      }),
      refreshPaths,
      {
        afterSuccess: () => {
          pruneLoadedDirectories([normalizedSrcPath, normalizedDestPath]);
        },
      },
    );
  }, [pruneLoadedDirectories, runMutation, sessionId]);

  const deleteEntries = useCallback(async (
    paths: string[],
  ): Promise<SessionFilesListResponse[]> => {
    if (paths.length === 0) {
      const nextError = 'SESSION_FILES_DELETE_EMPTY: at least one path is required.';
      setError(nextError);
      throw new Error(nextError);
    }

    const normalizedPaths = paths.map((path) => normalizePath(path));
    const refreshPaths = uniquePaths([
      ...normalizedPaths.map((path) => getParentPath(path)),
      rootPathRef.current,
    ]);
    return runMutation(
      () => deleteSessionFiles(sessionId, { paths: normalizedPaths }),
      refreshPaths,
      {
        afterSuccess: () => {
          removeDeletedPathsFromFileTree(normalizedPaths);
        },
      },
    );
  }, [removeDeletedPathsFromFileTree, runMutation, sessionId]);

  const download = useCallback(async (path: string): Promise<void> => {
    assertEnabled();
    setError(null);
    try {
      triggerBrowserDownload(
        buildSessionFileDownloadUrl(sessionId, path),
        inferDownloadName(path),
      );
    } catch (err) {
      setError(getErrorMessage(err));
      throw err;
    }
  }, [assertEnabled, sessionId]);

  const getDownloadUrl = useCallback((path: string): string => {
    return buildSessionFileDownloadUrl(sessionId, path);
  }, [sessionId]);

  return {
    rootPath,
    directoryEntriesByPath,
    directoryLoadingPaths,
    loading,
    mutating,
    loadedOnce,
    error,
    ensureDirectoryLoaded,
    refresh,
    upload,
    mkdir,
    move,
    rename: move,
    delete: deleteEntries,
    download,
    getDownloadUrl,
  };
}
