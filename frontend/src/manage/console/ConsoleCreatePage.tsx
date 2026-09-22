import { ChevronRightIcon } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';

import { ErrorNote } from '@/components/shell';
import { ConsoleEditSections } from './ConsoleEditSections';
import { ConsoleRecordPage } from './ConsoleRecordPage';
import { splitAdvanced } from './editFields';
import type { EditSectionSpec } from './index';

/**
 * A record that does not exist yet, on its own page.
 *
 * The same fields, in the same sections, as the page that will read it once it
 * does. That is what docs/frontend-design.md §4 asks for: creating and
 * editing are the same act against the same shape, and giving one a page and
 * the other a 40%-wide overlay is the mode switch §4 exists to remove. §3's
 * reasoning carries over unchanged — a form wants its labels beside its
 * controls and its controls wide enough to read a value in, which is about
 * 750px, and a panel that wide leaves the list it covers too narrow to keep.
 *
 * One difference from the record page, and it is structural rather than
 * cosmetic: the cards here have no save of their own. A section owns its save
 * once the record exists, because each is an independent write against a live
 * row. Before it exists there is nothing to write into, so there is one act and
 * one control for it, beside the heading with the other operations on the
 * record.
 */
export function ConsoleCreatePage({
  title,
  lede,
  sections,
  draft,
  onDraftChange,
  idPrefix,
  invalidKeys,
  onInvalidChange,
  error,
  saving,
  createLabel,
  savingLabel,
  onCreate,
  onCancel,
  blockedReason,
  advancedKeys,
}: {
  title: React.ReactNode;
  /** What this record is, in a sentence. */
  lede?: React.ReactNode;
  sections: EditSectionSpec[];
  draft: Record<string, unknown>;
  onDraftChange: (draft: Record<string, unknown>) => void;
  /** Prefix for each control's id, so a label points at one field on one page. */
  idPrefix: string;
  invalidKeys: Set<string>;
  onInvalidChange: (key: string, invalid: boolean) => void;
  error?: string;
  saving?: boolean;
  createLabel: string;
  savingLabel?: string;
  onCreate: () => void;
  onCancel: () => void;
  /** What is missing, when the record cannot be created yet. */
  blockedReason?: string;
  /**
   * Field keys this page may fold away behind a disclosure. Omitted means no
   * folding — which is the right answer for a surface whose reader is already
   * choosing sandbox backends, and the wrong one for a first Agent.
   */
  advancedKeys?: Set<string>;
}) {
  const { t } = useTranslation();
  const [showAdvanced, setShowAdvanced] = useState(false);
  const blocked = invalidKeys.size > 0 || !!blockedReason;
  const { essential, advanced, advancedCount } = advancedKeys
    ? splitAdvanced(sections, advancedKeys)
    : { essential: sections, advanced: [], advancedCount: 0 };

  return (
    <ConsoleRecordPage
      title={title}
      lede={lede}
      actions={
        <>
          <Button variant="outline" disabled={saving} onClick={onCancel}>
            {t('common:cancel')}
          </Button>
          <Button disabled={saving || blocked} onClick={onCreate}>
            {saving ? (savingLabel ?? createLabel) : createLabel}
          </Button>
        </>
      }
    >
      {/* Named so the visual-grammar walk can tell a create page from a record
          page asked for a record called "new". `contents` keeps it out of the
          layout it marks. */}
      <div data-slot="create-page" className="contents">
        {error && <ErrorNote>{error}</ErrorNote>}
        {blockedReason && !error && (
          <p className="t-copy text-muted-foreground">{blockedReason}</p>
        )}
        <ConsoleEditSections
          sections={essential}
          draft={draft}
          onDraftChange={onDraftChange}
          idPrefix={idPrefix}
          invalidKeys={invalidKeys}
          onInvalidChange={onInvalidChange}
        />
        {advancedCount > 0 && (
          /* One disclosure for all of them, not one per card: what is folded
             is "everything you do not need to answer yet", and splitting that
             across cards would make the reader open several to find out there
             was nothing they wanted. */
          <Collapsible onOpenChange={setShowAdvanced} open={showAdvanced}>
            <CollapsibleTrigger
              render={
                <button
                  className="flex cursor-pointer items-center gap-1 border-none bg-transparent p-0 text-muted-foreground"
                  type="button"
                />
              }
            >
              <ChevronRightIcon
                className={cn('size-4 transition-transform', showAdvanced && 'rotate-90')}
              />
              {showAdvanced
                ? t('common:advanced_hide')
                : t('common:advanced_show', { count: advancedCount })}
            </CollapsibleTrigger>
            <CollapsibleContent>
              <ConsoleEditSections
                sections={advanced}
                draft={draft}
                onDraftChange={onDraftChange}
                idPrefix={idPrefix}
                invalidKeys={invalidKeys}
                onInvalidChange={onInvalidChange}
              />
            </CollapsibleContent>
          </Collapsible>
        )}
      </div>
    </ConsoleRecordPage>
  );
}
