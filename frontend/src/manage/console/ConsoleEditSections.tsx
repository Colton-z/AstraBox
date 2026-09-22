import { Fragment } from 'react';
import { useTranslation } from 'react-i18next';

import { ConsoleCard } from './ConsoleCard';
import { ConsoleFieldRow } from './ConsoleForm';
import { EditFieldControl } from './EditFieldControl';
import type { EditFieldSpec, EditSectionSpec } from './editFields';

/** What a page decides about one card, beyond the fields it holds. */
export type SectionCardProps = {
  /** One line on why these fields are one group. */
  intro?: React.ReactNode;
  /** The constraint a reader should know before changing them. */
  note?: React.ReactNode;
  dirty?: boolean;
  blocked?: boolean;
  blockedReason?: React.ReactNode;
  saving?: boolean;
  saved?: boolean;
  saveLabel?: string;
  revertLabel?: string;
  onSave?: () => void;
  onRevert?: () => void;
};

/**
 * A group of editable fields, as cards.
 *
 * The one place for the shape every console form is made of: a card per
 * section, a labelled row per field, and an invalid-key set threaded through
 * both. Record pages and the create page render from here instead of each
 * keeping a copy of that loop, which is how the shape drifts between them.
 *
 * Saving is not part of it. A record page gives each section its own save
 * because each is an independent write against a live row (§4); a create page
 * has one act and one control for it. So the page says what a card carries
 * beyond its fields, through `cardProps`, and this renders the fields.
 */
export function ConsoleEditSections({
  sections,
  draft,
  onDraftChange,
  idPrefix,
  invalidKeys,
  onInvalidChange,
  cardProps,
  renderField,
}: {
  sections: EditSectionSpec[];
  draft: Record<string, unknown>;
  onDraftChange: (draft: Record<string, unknown>) => void;
  /** Prefix for each control's id, so a label points at one field on one page. */
  idPrefix: string;
  invalidKeys: Set<string>;
  onInvalidChange: (key: string, invalid: boolean) => void;
  cardProps?: (section: EditSectionSpec, index: number) => SectionCardProps;
  renderField?: (field: EditFieldSpec, row: React.ReactNode) => React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <>
      {sections.map((section, i) => (
        <ConsoleCard key={i} title={section.label} {...(cardProps?.(section, i) ?? {})}>
          {section.fields.map((f) => {
            const row = (
              <ConsoleFieldRow
                label={f.label}
                htmlFor={`${idPrefix}-${f.key}`}
                labelsGroup={f.type === 'list'}
                required={f.required}
                idTag={f.idTag}
                help={f.example && f.editable !== false && !f.disabled ? (
                  <>{f.help}{f.help && ' '}{t('manage:console.example_tab_hint')}</>
                ) : f.help}
                error={invalidKeys.has(f.key) ? t('manage:console.json_invalid') : undefined}
              >
                <EditFieldControl
                  id={`${idPrefix}-${f.key}`}
                  field={f}
                  draft={draft}
                  onDraftChange={onDraftChange}
                  onInvalid={onInvalidChange}
                  // The line under the field and the border around it are one
                  // complaint: a field that will not save must not look exactly
                  // like the ones that will.
                  invalid={invalidKeys.has(f.key)}
                />
              </ConsoleFieldRow>
            );
            return <Fragment key={f.key}>{renderField ? renderField(f, row) : row}</Fragment>;
          })}
        </ConsoleCard>
      ))}
    </>
  );
}
