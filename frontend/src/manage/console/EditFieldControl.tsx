import { Lock } from 'lucide-react';

import { InputGroup, InputGroupAddon, InputGroupText } from '@/components/ui/input-group';

import {
  ConsoleJsonField,
  ConsoleListField,
  ConsoleNumberField,
  ConsoleSearchSelect,
  ConsoleSelect,
  ConsoleTextArea,
  ConsoleTextField,
  ConsoleToggle,
} from './ConsoleForm';
import type { EditFieldSpec } from './editFields';

/** Render one edit field's control (or a read-only mono value if not editable). */
export function EditFieldControl({
  id,
  field,
  draft,
  onDraftChange,
  onInvalid,
  invalid,
}: {
  /**
   * Minted by the row above, so the same value reaches the label and the
   * control. Minting it here instead would leave the row's `htmlFor` pointing
   * at an id that only comes into existence one level below it.
   */
  id: string;
  field: EditFieldSpec;
  draft: Record<string, unknown>;
  onDraftChange: (next: Record<string, unknown>) => void;
  onInvalid: (key: string, invalid: boolean) => void;
  /** The field's own rule is complaining — mark the control, not just the text. */
  invalid?: boolean;
}) {
  const raw = field.get(draft);
  const set = (v: unknown) => onDraftChange(field.set(draft, v));

  const editable = field.editable !== false;
  const disabled = field.disabled === true;
  // The row above names its label with this id; a control that cannot be
  // reached by `<label for>` is named by pointing back at it.
  const labelledBy = `${id}-label`;

  if (!editable) {
    const empty = raw == null || (typeof raw === 'string' && raw.trim() === '');
    // Shaped like the control it replaces, with a lock and a sunken fill. Bare
    // text beside real inputs reads as an input that failed to render, leaving
    // the reader unable to tell "you may not change this" from "something is
    // broken".
    return (
      <InputGroup className="border-dashed bg-muted dark:bg-muted">
        <InputGroupAddon>
          <Lock className="opacity-60" aria-hidden />
        </InputGroupAddon>
        <InputGroupText className="min-w-0 flex-1 truncate pr-2.5 font-mono">
          {empty ? '—' : String(raw)}
        </InputGroupText>
      </InputGroup>
    );
  }

  switch (field.type) {
    case 'toggle':
      return (
        <ConsoleToggle
          id={id}
          labelledBy={labelledBy}
          checked={raw === true}
          onChange={(v) => set(v)}
          disabled={disabled}
          onLabel={field.onLabel}
          offLabel={field.offLabel}
        />
      );
    case 'select':
      return (
        <ConsoleSelect
          id={id}
          value={raw == null ? '' : String(raw)}
          onChange={(v) => set(v)}
          options={field.options ?? []}
          placeholder={field.placeholder}
          disabled={disabled}
          invalid={invalid}
        />
      );
    case 'search_select':
      return (
        <ConsoleSearchSelect
          id={id}
          value={raw == null ? '' : String(raw)}
          onChange={(v) => set(v)}
          options={field.options ?? []}
          placeholder={field.placeholder}
          searchPlaceholder={field.searchPlaceholder}
          emptyLabel={field.emptyLabel}
          allowCustom={field.allowCustom}
          disabled={disabled}
        />
      );
    case 'number':
      return (
        <ConsoleNumberField
          id={id}
          value={typeof raw === 'number' ? raw : ''}
          onChange={(v) => set(v === '' ? '' : v)}
          placeholder={field.placeholder}
          example={field.example}
          disabled={disabled}
          invalid={invalid}
        />
      );
    case 'textarea':
      return (
        <ConsoleTextArea
          id={id}
          value={raw == null ? '' : String(raw)}
          onChange={(v) => set(v)}
          placeholder={field.placeholder}
          example={field.example}
          rows={field.rows ?? 3}
          mono={field.mono}
          disabled={disabled}
          invalid={invalid}
        />
      );
    case 'list':
      return (
        <ConsoleListField
          labelledBy={labelledBy}
          value={(Array.isArray(raw) ? raw : []).map(String)}
          onChange={(v) => set(v)}
          placeholder={field.placeholder}
          example={field.example}
          disabled={disabled}
        />
      );
    case 'json':
      return (
        <ConsoleJsonField
          id={id}
          value={raw}
          placeholder={field.placeholder}
          example={field.example}
          rows={field.rows ?? 5}
          onChange={(v) => set(v)}
          onValidityChange={(ok) => onInvalid(field.key, !ok)}
          disabled={disabled}
        />
      );
    case 'password':
      return (
        <ConsoleTextField
          id={id}
          type="password"
          value={raw == null ? '' : String(raw)}
          onChange={(v) => set(v)}
          placeholder={field.placeholder}
          example={field.example}
          disabled={disabled}
        />
      );
    case 'text':
    default:
      return (
        <ConsoleTextField
          id={id}
          value={raw == null ? '' : String(raw)}
          onChange={(v) => set(v)}
          placeholder={field.placeholder}
          example={field.example}
          disabled={disabled}
          invalid={invalid}
        />
      );
  }
}
