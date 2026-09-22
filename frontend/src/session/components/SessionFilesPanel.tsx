import { type ChangeEvent, type DragEvent, type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { EmptyState, ErrorNote } from '@/components/shell';
import {
  FileTree,
  FileTreeActions,
  FileTreeFile,
  FileTreeIcon,
  FileTreeName,
} from '@/components/ai-elements/file-tree';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { ScrollArea } from '@/components/ui/scroll-area';
import { cn } from '@/lib/utils';
import {
  ChevronRightIcon,
  DownloadIcon,
  FileIcon,
  FolderIcon,
  FolderOpenIcon,
  FolderPlusIcon,
  MoreHorizontalIcon,
  RefreshCcwIcon,
  UploadIcon,
} from 'lucide-react';
import type { SessionFileEntry } from '../../types';
import { useSessionFiles } from '../hooks/useSessionFiles';

type SessionFilesPanelProps = {
  sessionId: string;
  enabled?: boolean;
  disabledMessage?: string;
  // Bumped by the parent when something may have changed the workspace (e.g. a
  // turn just ended); each new value refetches the open directories once.
  refreshSignal?: number;
};

function normalizePath(path: string): string {
  if (path === '/') {
    return '/';
  }
  return path.replace(/\/+$/, '');
}

function joinPath(base: string, leaf: string): string {
  const cleanLeaf = String(leaf || '').trim().replace(/^\/+/, '');
  if (!cleanLeaf) {
    return normalizePath(base);
  }
  const normalizedBase = normalizePath(base);
  if (normalizedBase === '/') {
    return `/${cleanLeaf}`;
  }
  return `${normalizedBase}/${cleanLeaf}`;
}

function dirname(path: string): string | null {
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

function basename(path: string): string {
  const normalizedPath = normalizePath(path);
  if (!normalizedPath || normalizedPath === '/') {
    return '/';
  }
  const lastSlashIndex = normalizedPath.lastIndexOf('/');
  return lastSlashIndex >= 0 ? normalizedPath.slice(lastSlashIndex + 1) : normalizedPath;
}

function formatBytes(size?: number | null): string {
  if (size === null || size === undefined || Number.isNaN(size)) {
    return '';
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  if (size < 1024 * 1024 * 1024) {
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function isSameOrDescendantPath(path: string, ancestorPath: string): boolean {
  const normalizedPath = normalizePath(path);
  const normalizedAncestorPath = normalizePath(ancestorPath);
  return normalizedPath === normalizedAncestorPath
    || normalizedPath.startsWith(`${normalizedAncestorPath}/`);
}

function replacePathPrefix(path: string, sourcePath: string, destPath: string): string {
  const normalizedPath = normalizePath(path);
  const normalizedSourcePath = normalizePath(sourcePath);
  const normalizedDestPath = normalizePath(destPath);

  if (normalizedPath === normalizedSourcePath) {
    return normalizedDestPath;
  }
  if (!normalizedPath.startsWith(`${normalizedSourcePath}/`)) {
    return normalizedPath;
  }
  return `${normalizedDestPath}${normalizedPath.slice(normalizedSourcePath.length)}`;
}

function buildEntryMap(directoryEntriesByPath: Record<string, SessionFileEntry[]>): Map<string, SessionFileEntry> {
  const entryMap = new Map<string, SessionFileEntry>();
  for (const entries of Object.values(directoryEntriesByPath)) {
    for (const entry of entries) {
      entryMap.set(entry.path, entry);
    }
  }
  return entryMap;
}

function resolveActiveDirectoryPath(
  rootPath: string | null,
  selectedPath: string | undefined,
  entryByPath: Map<string, SessionFileEntry>,
): string | null {
  if (!rootPath) {
    return null;
  }
  if (!selectedPath || selectedPath === rootPath) {
    return rootPath;
  }
  const selectedEntry = entryByPath.get(selectedPath);
  if (selectedEntry?.kind === 'directory') {
    return selectedEntry.path;
  }
  return dirname(selectedPath) ?? rootPath;
}

function EntryActions({
  entry,
  allowDownload,
  onDownload,
  onRename,
  onDelete,
}: {
  entry: SessionFileEntry;
  allowDownload?: boolean;
  onDownload: (entry: SessionFileEntry) => void;
  onRename: (entry: SessionFileEntry) => void;
  onDelete: (entry: SessionFileEntry) => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      {allowDownload ? (
        <Button
          className="h-7 w-7"
          size="icon"
          title={t('chat:files.download')}
          variant="ghost"
          onClick={() => onDownload(entry)}
        >
          <DownloadIcon className="size-4" />
        </Button>
      ) : null}
      <DropdownMenu>
        <DropdownMenuTrigger
          render={(
            <Button
              className="h-7 w-7"
              size="icon"
              title={t('chat:files.more_actions')}
              variant="ghost"
            />
          )}
        >
          <MoreHorizontalIcon className="size-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {allowDownload ? (
            <DropdownMenuItem onClick={() => onDownload(entry)}>
              {t('chat:files.download')}
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuItem onClick={() => onRename(entry)}>{t('chat:files.rename')}</DropdownMenuItem>
          <DropdownMenuItem
            className="text-destructive focus:text-destructive"
            onClick={() => onDelete(entry)}
          >
            {t('common:delete')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}

/**
 * One directory in the tree: a disclosure chevron, the folder's name, an
 * optional label beside it, and the row's own actions.
 *
 * `FileTreeFolder` from `@/components/ai-elements/file-tree` draws a fixed row —
 * chevron, icon, name — inside a div it closes itself, so a caller has nowhere
 * to put the rename/delete menu or the "Root directory" label. Composing the row
 * here is what gives those two a place; the leaf rows stay on the vendored
 * `FileTreeFile`, which takes children.
 *
 * Expansion and selection arrive as props rather than through `FileTree`'s
 * context, which is private to that module. `SessionFilesPanel` owns both sets
 * of state anyway, because expanding a directory is also what fetches it.
 */
function DirectoryRow({
  path,
  name,
  meta,
  actions,
  expanded,
  selected,
  onToggle,
  onSelect,
  children,
}: {
  path: string;
  name: string;
  meta?: ReactNode;
  actions?: ReactNode;
  expanded: boolean;
  selected: boolean;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
  children: ReactNode;
}) {
  return (
    <Collapsible onOpenChange={() => onToggle(path)} open={expanded}>
      {/* The node of the `role="tree"` that `FileTree` opens: the row and the
          children it discloses are one treeitem, so a reader on assistive
          technology hears the directory and its contents as one branch. */}
      <div className="w-full min-w-0 max-w-full" role="treeitem" tabIndex={0}>
        <div
          className={cn(
            'flex w-full min-w-0 items-center gap-1 rounded px-2 py-1 text-left transition-colors hover:bg-muted/50',
            selected && 'bg-muted',
          )}
        >
          {/* Two controls, not one: the chevron discloses the directory and the
              name selects it. Looking inside a directory is not the same act as
              choosing where the next upload and the next new folder land. */}
          <CollapsibleTrigger
            render={(
              <button
                className="flex shrink-0 cursor-pointer items-center border-none bg-transparent p-0"
                type="button"
              />
            )}
          >
            <ChevronRightIcon
              className={cn(
                'size-4 shrink-0 text-muted-foreground transition-transform',
                expanded && 'rotate-90',
              )}
            />
          </CollapsibleTrigger>
          <button
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-1 border-none bg-transparent p-0 text-left"
            onClick={() => onSelect(path)}
            type="button"
          >
            <FileTreeIcon>
              {expanded ? (
                <FolderOpenIcon className="size-4 text-muted-foreground" />
              ) : (
                <FolderIcon className="size-4 text-muted-foreground" />
              )}
            </FileTreeIcon>
            <FileTreeName>{name}</FileTreeName>
            {meta ? (
              <span className="shrink-0 text-11 text-muted-foreground">{meta}</span>
            ) : null}
          </button>
          {actions ? <FileTreeActions>{actions}</FileTreeActions> : null}
        </div>
        <CollapsibleContent>
          <div className="ml-4 min-w-0 border-l pl-2">{children}</div>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

export default function SessionFilesPanel({
  sessionId,
  enabled = true,
  disabledMessage,
  refreshSignal,
}: SessionFilesPanelProps) {
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedPath, setSelectedPath] = useState<string>();
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [dragActive, setDragActive] = useState(false);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [renameEntry, setRenameEntry] = useState<SessionFileEntry | null>(null);
  const [renameName, setRenameName] = useState('');
  const [deleteEntry, setDeleteEntry] = useState<SessionFileEntry | null>(null);
  const {
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
    rename,
    delete: deleteEntries,
    download,
  } = useSessionFiles(sessionId, { enabled });

  // Refetch the open directories whenever the parent bumps `refreshSignal`
  // (e.g. a turn just ended and the agent may have written files). Hold the
  // latest `refresh` in a ref so this effect depends only on the signal — not
  // on `refresh`'s identity (which churns with enabled/sessionId) — and skip
  // the initial value so it never double-fetches the hook's own mount load.
  const refreshRef = useRef(refresh);
  const lastRefreshSignalRef = useRef(refreshSignal);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);
  useEffect(() => {
    if (refreshSignal === lastRefreshSignalRef.current) {
      return;
    }
    lastRefreshSignalRef.current = refreshSignal;
    if (!enabled) {
      return;
    }
    void refreshRef.current({ force: true, background: true }).catch(() => {});
  }, [refreshSignal, enabled]);

  const entryByPath = useMemo(
    () => buildEntryMap(directoryEntriesByPath),
    [directoryEntriesByPath],
  );
  const activeDirectoryPath = useMemo(
    () => resolveActiveDirectoryPath(rootPath, selectedPath, entryByPath),
    [entryByPath, rootPath, selectedPath],
  );
  const rootEntries = rootPath ? (directoryEntriesByPath[rootPath] ?? []) : [];
  const busy = !enabled || loading || mutating;
  const rootLabel = rootPath ? basename(rootPath) : 'workspace';
  // A path, or nothing. The reason there is no path belongs to the empty
  // state below, which says it in full — carrying it here as well would state
  // the unavailability twice, in two sizes, one above the other (§1).
  const directoryDisplayPath = enabled
    ? (activeDirectoryPath ?? rootPath ?? t('chat:files.loading_directory'))
    : '—';
  const disabledPanelMessage = disabledMessage || t('chat:files.unavailable_session_not_ready');

  useEffect(() => {
    setSelectedPath(undefined);
    setExpandedPaths(new Set());
    setDragActive(false);
    setCreateDialogOpen(false);
    setCreateName('');
    setRenameEntry(null);
    setRenameName('');
    setDeleteEntry(null);
  }, [sessionId]);

  useEffect(() => {
    if (!rootPath) {
      return;
    }
    setExpandedPaths((current) => (current.size === 0 ? new Set([rootPath]) : current));
    setSelectedPath((current) => current ?? rootPath);
  }, [rootPath]);

  useEffect(() => {
    if (!rootPath || !selectedPath || selectedPath === rootPath) {
      return;
    }
    if (entryByPath.has(selectedPath)) {
      return;
    }
    setSelectedPath(dirname(selectedPath) ?? rootPath);
  }, [entryByPath, rootPath, selectedPath]);

  useEffect(() => {
    if (!rootPath) {
      return;
    }
    setExpandedPaths((current) => {
      const next = new Set(
        Array.from(current).filter((path) => path === rootPath || entryByPath.has(path)),
      );
      return next.size === current.size ? current : next;
    });
  }, [entryByPath, rootPath]);

  const handleSelect = async (path: string) => {
    if (!enabled) {
      return;
    }
    setSelectedPath(path);
    if (path === rootPath || entryByPath.get(path)?.kind === 'directory') {
      await ensureDirectoryLoaded(path, { force: false });
    }
  };

  const handleExpandedChange = (nextExpandedPaths: Set<string>) => {
    if (!enabled) {
      return;
    }
    const addedPaths = Array.from(nextExpandedPaths).filter((path) => !expandedPaths.has(path));
    setExpandedPaths(new Set(nextExpandedPaths));
    for (const path of addedPaths) {
      void ensureDirectoryLoaded(path, { force: false });
    }
  };

  const handleToggle = (path: string) => {
    const nextExpandedPaths = new Set(expandedPaths);
    if (nextExpandedPaths.has(path)) {
      nextExpandedPaths.delete(path);
    } else {
      nextExpandedPaths.add(path);
    }
    handleExpandedChange(nextExpandedPaths);
  };

  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const handleUploadChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const fileList = event.target.files;
    if (!fileList || fileList.length === 0 || !activeDirectoryPath) {
      return;
    }
    try {
      await upload(fileList, { path: activeDirectoryPath });
    } finally {
      event.target.value = '';
    }
  };

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (busy || !event.dataTransfer.types.includes('Files')) {
      return;
    }
    event.preventDefault();
    setDragActive(true);
  };

  const handleDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (busy || !event.dataTransfer.types.includes('Files')) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    if (!dragActive) {
      setDragActive(true);
    }
  };

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    if (!dragActive) {
      return;
    }
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) {
      return;
    }
    setDragActive(false);
  };

  const handleDrop = async (event: DragEvent<HTMLDivElement>) => {
    if (busy || !event.dataTransfer.files || event.dataTransfer.files.length === 0 || !activeDirectoryPath) {
      return;
    }
    event.preventDefault();
    setDragActive(false);
    await upload(event.dataTransfer.files, { path: activeDirectoryPath });
  };

  const handleRefreshClick = () => {
    void refresh({ force: true }).catch(() => {});
  };

  const openCreateDialog = () => {
    if (!activeDirectoryPath) {
      return;
    }
    setCreateName('');
    setCreateDialogOpen(true);
  };

  const submitCreateDirectory = async (event?: FormEvent<HTMLFormElement>) => {
    event?.preventDefault();
    if (!activeDirectoryPath) {
      return;
    }
    const name = createName.trim();
    if (!name) {
      return;
    }
    const createdPath = joinPath(activeDirectoryPath, name);
    await mkdir(createdPath);
    setExpandedPaths((current) => new Set(current).add(activeDirectoryPath));
    setCreateDialogOpen(false);
    setCreateName('');
  };

  const openRenameDialog = (entry: SessionFileEntry) => {
    setRenameEntry(entry);
    setRenameName(entry.name);
  };

  const submitRename = async (event?: FormEvent<HTMLFormElement>) => {
    event?.preventDefault();
    const entry = renameEntry;
    if (!entry) {
      return;
    }
    const parentPath = dirname(entry.path) ?? rootPath ?? '/';
    const nextName = renameName.trim();
    if (!nextName || nextName === entry.name) {
      return;
    }

    const destPath = joinPath(parentPath, nextName);
    await rename(entry.path, destPath);

    setSelectedPath((current) => (
      current && isSameOrDescendantPath(current, entry.path)
        ? replacePathPrefix(current, entry.path, destPath)
        : current
    ));

    if (entry.kind === 'directory') {
      setExpandedPaths((current) => {
        const next = new Set<string>();
        let changed = false;
        for (const path of current) {
          if (isSameOrDescendantPath(path, entry.path)) {
            next.add(replacePathPrefix(path, entry.path, destPath));
            changed = true;
          } else {
            next.add(path);
          }
        }
        return changed ? next : current;
      });
    }
    setRenameEntry(null);
    setRenameName('');
  };

  const openDeleteDialog = (entry: SessionFileEntry) => {
    setDeleteEntry(entry);
  };

  const submitDelete = async () => {
    const entry = deleteEntry;
    if (!entry) {
      return;
    }

    await deleteEntries([entry.path]);

    setSelectedPath((current) => {
      if (!current || !isSameOrDescendantPath(current, entry.path)) {
        return current;
      }
      return dirname(entry.path) ?? rootPath ?? current;
    });

    if (entry.kind === 'directory') {
      setExpandedPaths((current) => {
        const next = new Set(
          Array.from(current).filter((path) => !isSameOrDescendantPath(path, entry.path)),
        );
        return next.size === current.size ? current : next;
      });
    }
    setDeleteEntry(null);
  };

  const renderDirectoryChildren = (directoryPath: string) => {
    const entries = directoryEntriesByPath[directoryPath] ?? [];
    const isDirectoryLoading = directoryLoadingPaths.has(directoryPath);

    if (entries.length > 0) {
      return entries.map((entry) => {
        if (entry.kind === 'directory') {
          return (
            <DirectoryRow
              actions={(
                <EntryActions
                  entry={entry}
                  onDelete={openDeleteDialog}
                  onDownload={() => undefined}
                  onRename={openRenameDialog}
                />
              )}
              expanded={expandedPaths.has(entry.path)}
              key={entry.path}
              name={entry.name}
              onSelect={(path) => {
                void handleSelect(path);
              }}
              onToggle={handleToggle}
              path={entry.path}
              selected={selectedPath === entry.path}
            >
              {renderDirectoryChildren(entry.path)}
            </DirectoryRow>
          );
        }

        return (
          <FileTreeFile
            className="w-full min-w-0 max-w-full"
            key={entry.path}
            name={entry.name}
            path={entry.path}
          >
            <span className="size-4 shrink-0" />
            <FileTreeIcon>
              <FileIcon className="size-4 text-muted-foreground" />
            </FileTreeIcon>
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <FileTreeName>{entry.name}</FileTreeName>
              <span className="shrink-0 text-11 text-muted-foreground">
                {formatBytes(entry.size)}
              </span>
            </div>
            <FileTreeActions>
              <EntryActions
                allowDownload
                entry={entry}
                onDelete={openDeleteDialog}
                onDownload={(fileEntry) => {
                  void download(fileEntry.path);
                }}
                onRename={openRenameDialog}
              />
            </FileTreeActions>
          </FileTreeFile>
        );
      });
    }

    if (isDirectoryLoading) {
      return (
        <div className="px-3 py-2 text-xs text-muted-foreground">
          {t('chat:files.loading')}
        </div>
      );
    }

    return (
      <div className="px-3 py-2 text-xs text-muted-foreground">
        {t('chat:files.empty_folder')}
      </div>
    );
  };

  return (
    <div
      className={cn(
        'relative flex h-full min-h-0 flex-col',
        dragActive && 'bg-accent/20',
      )}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={(event) => {
        void handleDrop(event);
      }}
    >
      {dragActive ? (
        <div className="pointer-events-none absolute inset-3 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-primary bg-background/85 text-sm font-medium text-foreground shadow-sm">
          {t('chat:files.drop_to_upload', { dir: activeDirectoryPath ?? rootPath ?? t('chat:files.current_directory') })}
        </div>
      ) : null}

      <div className="flex flex-wrap items-start gap-2 border-b px-3 py-2">
        <div className="min-w-0 flex-[1_1_12rem]">
          <div className="text-11 text-muted-foreground">{t('chat:files.current_directory')}</div>
          <div className="break-all font-mono text-11 leading-4 text-muted-foreground">
            {directoryDisplayPath}
          </div>
        </div>
        <div className="flex max-w-full shrink-0 flex-wrap items-center justify-end gap-2">
          <Button disabled={busy || !activeDirectoryPath} onClick={handleUploadClick} size="sm" variant="outline">
            <UploadIcon className="mr-1 size-4" />
            {t('chat:files.upload')}
          </Button>
          <Button disabled={busy || !activeDirectoryPath} onClick={openCreateDialog} size="sm" variant="outline">
            <FolderPlusIcon className="mr-1 size-4" />
            {t('common:new')}
          </Button>
          <Button
            disabled={busy}
            onClick={handleRefreshClick}
            aria-label={t('common:refresh')}
            size="icon"
            variant="ghost"
          >
            <RefreshCcwIcon className="size-4" />
          </Button>
        </div>
        <input
          className="hidden"
          multiple
          onChange={handleUploadChange}
          ref={fileInputRef}
          type="file"
        />
      </div>

      {error ? (
        // The strip spans the panel head and carries a rule instead of a box,
        // so the note's own frame is flattened to its bottom edge. What it is
        // here for is the role: an upload, rename or delete that fails has to
        // be announced, not just coloured.
        <ErrorNote className="rounded-none border-x-0 border-t-0 bg-destructive/5 px-3 py-2 text-xs">
          {error}
        </ErrorNote>
      ) : null}

      {/* The panel's own width discipline lives on the content box below: the
          viewport puts its children in the scroll box directly, so `w-full
          min-w-0 max-w-full` there is what keeps a long file name truncating
          instead of widening the tree. */}
      <ScrollArea className="min-h-0 flex-1">
        <div className="box-border w-full min-w-0 max-w-full p-3">
          {enabled && rootPath ? (
            // `selectedPath` and `onSelect` are what `FileTree` passes down to
            // the leaf rows; the directory rows are `DirectoryRow`, which takes
            // its expansion and selection as props, so the tree's own
            // `expanded`/`onExpandedChange` pair would reach nothing.
            <FileTree
              className="box-border w-full min-w-0 max-w-full rounded-xl border"
              onSelect={(path) => {
                void handleSelect(path);
              }}
              selectedPath={selectedPath}
            >
              <DirectoryRow
                expanded={expandedPaths.has(rootPath)}
                meta={t('chat:files.root_directory')}
                name={rootLabel}
                onSelect={(path) => {
                  void handleSelect(path);
                }}
                onToggle={handleToggle}
                path={rootPath}
                selected={selectedPath === rootPath}
              >
                {renderDirectoryChildren(rootPath)}
              </DirectoryRow>
            </FileTree>
          ) : (
            <EmptyState
              icon={<FolderIcon className="size-5" />}
              title={enabled ? (loading ? t('chat:files.loading_directory') : t('chat:files.directory_unavailable')) : disabledPanelMessage}
              hint={enabled ? (loading || !loadedOnce ? t('chat:files.please_wait') : t('chat:files.refresh_to_retry')) : t('chat:files.wait_for_runtime')}
            />
          )}

          {enabled && rootPath && rootEntries.length === 0 && !directoryLoadingPaths.has(rootPath) ? (
            <EmptyState
              className="mt-3"
              icon={<FolderIcon className="size-5" />}
              title={t('chat:files.current_directory_empty')}
              hint={t('chat:files.empty_directory_hint')}
              action={
                <div className="flex flex-wrap justify-center gap-2">
                <Button disabled={busy || !activeDirectoryPath} onClick={handleUploadClick} size="sm" variant="outline">
                  {t('chat:files.upload_file')}
                </Button>
                <Button disabled={busy || !activeDirectoryPath} onClick={openCreateDialog} size="sm" variant="outline">
                  {t('chat:files.new_folder')}
                </Button>
                </div>
              }
            />
          ) : null}
        </div>
      </ScrollArea>

      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('chat:files.new_folder')}</DialogTitle>
            <DialogDescription>{t('chat:files.new_folder_description')}</DialogDescription>
          </DialogHeader>
          <form className="grid gap-4" onSubmit={(event) => void submitCreateDirectory(event)}>
            <Input
              autoFocus
              placeholder={t('chat:files.folder_name_placeholder')}
              value={createName}
              onChange={(event) => setCreateName(event.target.value)}
            />
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setCreateDialogOpen(false)}>
                {t('common:cancel')}
              </Button>
              <Button disabled={!enabled || !createName.trim() || mutating} type="submit">
                {t('common:confirm')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={renameEntry !== null} onOpenChange={(open) => {
        if (!open) {
          setRenameEntry(null);
          setRenameName('');
        }
      }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('chat:files.rename')}</DialogTitle>
            <DialogDescription>{t('chat:files.rename_description')}</DialogDescription>
          </DialogHeader>
          <form className="grid gap-4" onSubmit={(event) => void submitRename(event)}>
            <Input
              autoFocus
              placeholder={t('chat:files.new_name_placeholder')}
              value={renameName}
              onChange={(event) => setRenameName(event.target.value)}
            />
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setRenameEntry(null);
                  setRenameName('');
                }}
              >
                {t('common:cancel')}
              </Button>
              <Button
                disabled={!enabled || !renameName.trim() || renameName.trim() === renameEntry?.name || mutating}
                type="submit"
              >
                {t('common:confirm')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* The only one of the three that is a question rather than a form: the
          delete takes effect on the remote workspace with no undo, so it is an
          `alertdialog` — announced as an interruption, modal, and not
          dismissable by a click outside it. The dialog is controlled because
          the menu item that opens it unmounts with its menu, leaving no element
          to be a trigger. */}
      <AlertDialog open={deleteEntry !== null} onOpenChange={(open) => {
        if (!open) {
          setDeleteEntry(null);
        }
      }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('common:delete')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('chat:files.delete_confirm', { name: deleteEntry?.name ?? t('chat:files.this_item') })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common:cancel')}</AlertDialogCancel>
            <AlertDialogAction
              disabled={!enabled || mutating}
              variant="destructive"
              onClick={() => {
                void submitDelete();
              }}
            >
              {t('common:delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
