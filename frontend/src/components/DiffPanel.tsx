import { FileDiffIcon } from 'lucide-react';
import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import { EmptyState } from '@/components/shell';
import { Item, ItemActions, ItemContent, ItemTitle } from '@/components/ui/item';
import { cn } from '@/lib/utils';
import type { FileChange } from '../types';
import { fileDiffLines } from '../session/fileChanges';

export default function DiffPanel({ fileChanges, selectedFile, onSelectFile }: {
  fileChanges: FileChange[];
  selectedFile: string | null;
  onSelectFile: (path: string) => void;
}) {
  const { t } = useTranslation();
  // Aggregate: latest change per file path
  const fileMap = useMemo(() => {
    const map = new Map<string, FileChange>();
    for (const fc of fileChanges) {
      map.set(fc.filePath, fc);
    }
    return map;
  }, [fileChanges]);

  const files = useMemo(() => Array.from(fileMap.values()).sort((a, b) => b.timestamp - a.timestamp), [fileMap]);
  const totalAdded = files.reduce((s, f) => s + f.linesAdded, 0);
  const totalRemoved = files.reduce((s, f) => s + f.linesRemoved, 0);
  const selected = selectedFile ? fileMap.get(selectedFile) : files[0];

  const selectedDiff = useMemo(() => {
    if (!selected) return [];
    return fileDiffLines(selected.diff);
  }, [selected]);

  if (files.length === 0) {
    return (
      <EmptyState
        icon={<FileDiffIcon className="size-5" />}
        title={t('misc:diff.empty_title')}
        hint={t('misc:diff.empty_hint')}
      />
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="px-3 py-2 text-xs text-muted-foreground border-b border-border bg-muted/50 shrink-0">
        {t('misc:diff.files_changed', { count: files.length })}
        {totalAdded > 0 && <span className="text-mint-fg"> +{totalAdded}</span>}
        {totalRemoved > 0 && <span className="text-crimson-fg"> -{totalRemoved}</span>}
      </div>
      {/* Each row picks the file the diff below shows, so each row is a
          button. A div with an `onClick` and a `cursor-pointer` promises a
          press that no Tab can reach and no screen reader is told about
          (docs/frontend-design.md §10). `Item` at the `xs` band is the same row
          the agents panel next to it uses, and it carries the focus ring — so
          the rows sit in a gap instead of tiling, and the ring lands on the
          panel and not 3px into the neighbour above (§11). */}
      <div className="flex shrink-0 max-h-40 flex-col gap-1 overflow-y-auto border-b border-border p-1">
        {files.map((f) => {
          const shortPath = f.filePath.split('/').slice(-3).join('/');
          const isActive = selected?.filePath === f.filePath;
          return (
            <Item
              key={f.filePath}
              size="xs"
              render={<button type="button" />}
              aria-current={isActive ? 'true' : undefined}
              onClick={() => onSelectFile(f.filePath)}
              className={cn('min-w-0 text-left hover:bg-accent', isActive && 'bg-accent font-medium')}
            >
              <ItemContent className="min-w-0">
                {/* `line-clamp-none` for the reason `AgentsListPanel` gives:
                    line clamping and `flex` both set a display on the same
                    element, and only stylesheet order picks between them. */}
                <ItemTitle className="w-full min-w-0 font-normal line-clamp-none">
                  <span className="min-w-0 flex-1 truncate text-foreground">{shortPath}</span>
                </ItemTitle>
              </ItemContent>
              <ItemActions className="gap-1.5 text-xs">
                {f.linesAdded > 0 && <span className="text-mint-fg">+{f.linesAdded}</span>}
                {f.linesRemoved > 0 && <span className="text-crimson-fg">-{f.linesRemoved}</span>}
              </ItemActions>
            </Item>
          );
        })}
      </div>
      {selected && (
        <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
          <div className="px-3 py-1.5 text-xs font-medium text-muted-foreground border-b border-border bg-muted/30 shrink-0">{selected.filePath}</div>
          <div data-testid="file-diff-content" className="flex-1 min-h-0 overflow-auto font-mono text-xs leading-5">
            {selected.diff === null && (
              <p className="p-3 text-muted-foreground">{t('misc:diff.unavailable')}</p>
            )}
            {selectedDiff.map((line, i) => (
                <div
                  key={i}
                  data-diff-kind={line.kind}
                  className={`flex ${line.kind === 'added' ? 'bg-mint/10 text-mint-fg' : line.kind === 'removed' ? 'bg-crimson/10 text-crimson-fg' : 'text-muted-foreground'}`}
                >
                  <span className="select-none w-5 shrink-0 text-center opacity-50">{line.kind === 'added' ? '+' : line.kind === 'removed' ? '-' : ' '}</span>
                  <span className="flex-1 whitespace-pre-wrap break-all px-1">{line.text}</span>
                </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
