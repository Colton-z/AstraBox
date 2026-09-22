import { diffLines, parsePatch } from 'diff';
import type { UIMessage as SDKUIMessage } from 'ai';
import type { FileChange, FileChangeDiff, FileChangesResult } from '../types';

export interface FileDiffLine {
  kind: 'added' | 'removed' | 'context' | 'hunk';
  text: string;
}

function contentLines(before: string, after: string): FileDiffLine[] {
  return diffLines(before, after).flatMap((part) =>
    part.value.replace(/\n$/, '').split('\n').map((text) => ({
      kind: part.added ? 'added' as const : part.removed ? 'removed' as const : 'context' as const,
      text,
    })));
}

// Keep native hunk boundaries; never reconstruct missing original file bytes.
export function fileDiffLines(diff: FileChangeDiff | null): FileDiffLine[] {
  if (diff === null) return [];
  if (diff.format === 'contents') return contentLines(diff.before, diff.after);
  if (diff.format === 'excerpts') return diff.excerpts.flatMap((excerpt, index) => [
    { kind: 'hunk' as const, text: `@@ ${index + 1} @@` },
    ...contentLines(excerpt.before, excerpt.after),
  ]);
  const hunks = diff.format === 'unified'
    ? parsePatch(diff.patch).flatMap((patch) => patch.hunks)
    : diff.hunks;
  return hunks.flatMap((hunk) => [
    { kind: 'hunk' as const, text: `@@ -${hunk.oldStart},${hunk.oldLines} +${hunk.newStart},${hunk.newLines} @@` },
    ...hunk.lines.map((line): FileDiffLine => ({
      kind: line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : 'context',
      text: line.startsWith('\\') ? line : line.slice(1),
    })),
  ]);
}

function results(messages: SDKUIMessage[]): FileChangesResult[] {
  const byId = new Map<string, FileChangesResult>();
  for (const message of messages) {
    if (message.role !== 'assistant') continue;
    for (const part of message.parts) {
      if (part.type !== 'data-file-changes') continue;
      const data = part.data as FileChangesResult;
      byId.set(`${message.id}:${data.toolCallId}`, data);
    }
  }
  return [...byId.values()];
}

// Counts stay cheap: patch parsing occurs only while Diff is open.
export function changedFilePaths(messages: SDKUIMessage[], enabled: boolean): string[] {
  return enabled ? [...new Set(results(messages).flatMap((result) => result.files.map((file) => file.path)))] : [];
}

export function computeFileChanges(messages: SDKUIMessage[], enabled: boolean): FileChange[] {
  if (!enabled) return [];
  return results(messages).flatMap((result, ordinal) => result.files.map((file) => {
    const lines = fileDiffLines(file.diff);
    return {
      filePath: file.path,
      toolName: result.toolName,
      diff: file.diff,
      linesAdded: lines.filter((line) => line.kind === 'added').length,
      linesRemoved: lines.filter((line) => line.kind === 'removed').length,
      timestamp: ordinal,
    };
  }));
}
