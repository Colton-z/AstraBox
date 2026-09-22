import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Check, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import {
  Combobox,
  ComboboxChip,
  ComboboxChips,
  ComboboxChipsInput,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
  ComboboxValue,
  useComboboxAnchor,
} from '@/components/ui/combobox';
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldError,
  FieldLabel,
} from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';

/**
 * Console form inputs — the edit face of every record and create page.
 *
 * Each control here is the kit's control, configured: `Input`, `Textarea`,
 * `NativeSelect`, `Switch` and `Combobox` from `components/ui`, wrapped in the
 * kit's `Field` grammar so a label, a help line and a validation message sit in
 * the same relationship on every page. What this file owns is the console's
 * layout decision (a label column beside its control from `md` up) and the
 * mapping from the console's field vocabulary onto those controls — not the
 * chrome of a field, which is the kit's.
 *
 * The set is intentionally small and config-mappable:
 *   text · textarea · number · select · toggle · list (string[])
 * Anything structurally richer (key_value / object / object_list) degrades to
 * {@link ConsoleJsonField} — a mono JSON escape hatch in the same grammar — so a
 * config can always render *something* editable without bespoke widgets.
 *
 * Mono is spent only where §6 allows it: a value the reader will type, paste or
 * match against a log line. That is the schema key beside a label, a JSON body,
 * and a code-ish textarea — not a dropdown's options and not an on/off legend.
 */

/** A labelled field shell: eyebrow label (+ optional required star) over control,
 *  with an optional mono `id` tag and a help/error line. The grammar wrapper every
 *  console control sits in, so inline-edit and create read identically. */
/**
 * What a row knows and its control has to announce.
 *
 * The row holds the required flag and the help and error text; the control is
 * its child, so it cannot be handed attributes directly. Published here and
 * read by the controls below, which is why no call site repeats them — and why
 * a control this file does not own simply goes without rather than breaking.
 */
const FieldAria = createContext<{ describedBy?: string; required?: boolean }>({});

/** The ARIA a control in a `ConsoleFieldRow` should spread onto its input. */
function useFieldAria() {
  const { describedBy, required } = useContext(FieldAria);
  return { 'aria-describedby': describedBy, 'aria-required': required || undefined };
}

export function ConsoleFieldRow({
  label,
  htmlFor,
  labelsGroup,
  required,
  idTag,
  help,
  error,
  children,
  className,
}: {
  label: React.ReactNode;
  htmlFor?: string;
  /** This row names a set of controls, not one — see ConsoleListField. */
  labelsGroup?: boolean;
  required?: boolean;
  /** Mono key tag shown after the label (e.g. the schema key) — the kit's id grammar. */
  idTag?: React.ReactNode;
  help?: React.ReactNode;
  error?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  const helpId = htmlFor && help ? `${htmlFor}-help` : undefined;
  const errorId = htmlFor && error ? `${htmlFor}-error` : undefined;
  // Error wins: when both are on screen the control points at the one that
  // says what to do next.
  const describedBy = errorId ?? helpId;

  // The wire key earns its place only when the reader could not have guessed it
  // from the label. Beside "Description" the tag `description` is the same word
  // twice, and a form of them reads as a page talking to itself in schema
  // (§6) — while beside "Engine", `engine_kind` is what an API payload and a
  // log line will actually say, which is the one thing the label cannot tell
  // them. So: show it when it differs from the label's own snake_case.
  const showIdTag =
    idTag != null &&
    idTag !== '' &&
    !(
      typeof idTag === 'string' &&
      typeof label === 'string' &&
      idTag === label.trim().toLowerCase().replace(/\s+/g, '_')
    );

  return (
    // Beside its control where there is room, stacked where there is not.
    // Side-by-side in a narrow column spends a third of the width on names two
    // words long and leaves the control too narrow to read its own value in — a
    // description renders as a three-line box with its own scrollbar, cut
    // mid-sentence. The breakpoint is the reference design's own.
    //
    // The kit's own `orientation="responsive"` is not this: it is a container
    // query answered by a `FieldGroup` ancestor, and it gives the label the
    // free width rather than a fixed column. Console rows are rendered directly
    // by pages that have no such ancestor, where it would never fire at all.
    <Field
      className={cn(
        'flex flex-col gap-1.5',
        'md:grid md:grid-cols-[13rem_minmax(0,1fr)] md:items-start md:gap-x-6 md:gap-y-1',
        className,
      )}
    >
      {/* The label element holds the label text and nothing else. A control
          named through `aria-labelledby` (the kit's combobox, a group) takes
          the whole referenced element as its name, aria-hidden descendants
          included, so the required marker and the wire key sit beside the
          label, not inside it — a required "Model" is named "Model", never
          "Model*".
          `flex-wrap`: the wire key is a single unbreakable-looking token, and
          on one row with the label it runs past a card narrower than the two
          of them. Wrapping gives it the row's full width.
          No alpha on the label colour: `--muted-foreground` is the metadata
          step and clears 4.5:1 on a card at full strength (5.23:1 light,
          6.75:1 dark). At 90% it measures 4.24:1 — a field label is a
          sentence, so it needs the whole step. */}
      <div className="flex flex-wrap gap-1.5 text-xs leading-snug text-muted-foreground md:pt-2">
        <span className="whitespace-nowrap">
          <FieldLabel
            id={htmlFor ? `${htmlFor}-label` : undefined}
            htmlFor={labelsGroup ? undefined : htmlFor}
            className="inline text-xs leading-snug font-normal text-muted-foreground"
          >
            {label}
          </FieldLabel>
          {required && (
            <span className="ml-1 text-astra-fg" aria-hidden="true">
              *
            </span>
          )}
        </span>
        {/* `select-text` against the kit label's `select-none`: the wire key is
            here to be copied into a payload or grepped for in a log. */}
        {showIdTag && (
          <span
            className="font-mono text-11 break-all text-muted-foreground select-text"
            aria-hidden="true"
          >
            {idTag}
          </span>
        )}
      </div>
      {/* The content column carries the field type, so a read-only value reads
          at exactly the size an editable one does — a control here is the kit's
          `text-sm`, and this is what a bare <p> in the same slot gets. Controls
          set their own size and are unaffected. */}
      <FieldContent className="min-w-0 gap-1.5 text-sm md:max-w-[35rem]">
        <FieldAria.Provider value={{ describedBy, required }}>
        {children}
        {/* The kit's error is a live region, so a message that arrives while
            the reader is looking at another field is still announced. */}
        </FieldAria.Provider>
        {error ? (
          <FieldError id={errorId} className="text-11 leading-snug">{error}</FieldError>
        ) : help ? (
          <FieldDescription id={helpId} className="text-11 leading-snug">{help}</FieldDescription>
        ) : null}
      </FieldContent>
    </Field>
  );
}

function acceptFieldExample(
  event: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>,
  example: string | undefined,
  accept: (value: string) => void,
) {
  if (
    event.key !== 'Tab' || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey
    || event.nativeEvent.isComposing || event.currentTarget.value !== '' || !example
  ) return;
  event.preventDefault();
  accept(example);
}

/** Single-line field. `mono` (default true) is right for ids/names/values. */
export function ConsoleTextField({
  id,
  value,
  onChange,
  placeholder,
  example,
  disabled,
  mono = true,
  invalid,
  inputMode,
  type = 'text',
}: {
  id?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  example?: string;
  disabled?: boolean;
  mono?: boolean;
  invalid?: boolean;
  inputMode?: React.HTMLAttributes<HTMLInputElement>['inputMode'];
  type?: 'text' | 'password';
}) {
  const aria = useFieldAria();
  return (
    <Input
      {...aria}
      id={id}
      type={type}
      className={cn(mono && 'font-mono')}
      value={value}
      placeholder={example ?? placeholder}
      disabled={disabled}
      inputMode={inputMode}
      aria-invalid={invalid || undefined}
      spellCheck={false}
      autoComplete={type === 'password' ? 'new-password' : 'off'}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => acceptFieldExample(e, example, onChange)}
    />
  );
}

/** Integer field. Emits '' when cleared (callers drop the key → backend default). */
export function ConsoleNumberField({
  id,
  value,
  onChange,
  placeholder,
  example,
  disabled,
  invalid,
}: {
  id?: string;
  value: number | '' | null | undefined;
  onChange: (v: number | '') => void;
  placeholder?: string;
  example?: string;
  disabled?: boolean;
  invalid?: boolean;
}) {
  const aria = useFieldAria();
  return (
    <Input
      {...aria}
      id={id}
      type="number"
      inputMode="numeric"
      className="font-mono tabular-nums"
      value={value == null ? '' : String(value)}
      placeholder={example ?? placeholder}
      disabled={disabled}
      aria-invalid={invalid || undefined}
      onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
      onKeyDown={(e) => acceptFieldExample(e, example, (v) => onChange(Number(v)))}
    />
  );
}

/** Multi-line field. `mono` for code-ish bodies (claude.md), false for prose. */
export function ConsoleTextArea({
  id,
  value,
  onChange,
  placeholder,
  example,
  disabled,
  rows = 3,
  mono = false,
  invalid,
}: {
  id?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  example?: string;
  disabled?: boolean;
  rows?: number;
  mono?: boolean;
  invalid?: boolean;
}) {
  const aria = useFieldAria();
  return (
    <Textarea
      {...aria}
      id={id}
      rows={rows}
      // `field-sizing-content` grows the box with what is typed and then
      // ignores `rows`; a schema that asks for eight rows is asking for the
      // room to be there before anything is in it.
      className={cn('field-sizing-fixed resize-y', mono && 'font-mono text-xs leading-5')}
      value={value}
      placeholder={example ?? placeholder}
      disabled={disabled}
      aria-invalid={invalid || undefined}
      spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => acceptFieldExample(e, example, onChange)}
    />
  );
}

export type ConsoleSelectOption = { value: string; label: React.ReactNode };

function optionText(option: ConsoleSelectOption): string {
  return typeof option.label === 'string' ? option.label : option.value;
}

/** One row of a searchable list: `label` is the string the kit filters on and
 *  writes into the input, `node` is what the row paints. The kit reads a
 *  `{ value, label }` shape without being told how. */
type ComboOption = { value: string; label: string; node: React.ReactNode };

function toComboOptions(options: ConsoleSelectOption[]): ComboOption[] {
  return options.map((option) => ({
    value: option.value,
    label: optionText(option),
    node: option.label,
  }));
}

/**
 * A searchable field is more than one input element, so it is named as a set.
 *
 * The kit's combobox renders the visible search box and an `aria-hidden` proxy
 * input beside it, the one that carries the value into a form submission. The
 * visible box is the control the reader operates and takes the row's
 * `<label for>`; nothing outside the primitive can put an attribute on the
 * proxy. So the pair is named the way {@link ConsoleListField} names its rows —
 * `for` names exactly one element, and this field has two.
 *
 * The naming text is `<id>-label`, which the row above mints for every field.
 */
function ComboboxFieldGroup({
  id,
  children,
}: {
  id?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="min-w-0"
      role={id ? 'group' : undefined}
      aria-labelledby={id ? `${id}-label` : undefined}
    >
      {children}
    </div>
  );
}

/** The popup both searchable fields open: one row per match, in one place so
 *  the single and multiple pickers cannot drift into two shapes. */
function ComboboxOptions({
  emptyLabel,
  anchor,
}: {
  emptyLabel: React.ReactNode;
  /** Chips grow taller than a line, so the popup hangs off them rather than
   *  off the input inside them. */
  anchor?: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <ComboboxContent anchor={anchor}>
      <ComboboxEmpty>{emptyLabel}</ComboboxEmpty>
      <ComboboxList>
        {(option: ComboOption) => (
          <ComboboxItem key={option.value} value={option}>
            <span className="min-w-0 truncate">{option.node}</span>
          </ComboboxItem>
        )}
      </ComboboxList>
    </ComboboxContent>
  );
}

/**
 * Matched on the id as well as the name — the kit's own filter reads the label.
 *
 * A console catalog is machine ids under human names, and the id is the half
 * the operator has in front of them, out of a log line or a payload. A search
 * that could not find `claude-opus-5` by typing it searches the wrong half.
 */
function comboMatches(option: ComboOption, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (q === '') return true;
  return `${option.value} ${option.label}`.toLowerCase().includes(q);
}

/** Searchable single select for catalogs whose option count is not small. */
export function ConsoleSearchSelect({
  id,
  value,
  onChange,
  options,
  placeholder,
  searchPlaceholder,
  emptyLabel,
  disabled,
  allowCustom = false,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  options: ConsoleSelectOption[];
  placeholder?: string;
  searchPlaceholder?: string;
  emptyLabel?: string;
  disabled?: boolean;
  allowCustom?: boolean;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');

  const known = useMemo(() => {
    const rows = toComboOptions(options);
    // An id the catalogue did not enumerate is still the record's value. Carry
    // it as a row of its own so the field shows what is stored rather than
    // blanking it, exactly as the native select does.
    if (value && !rows.some((option) => option.value === value)) {
      rows.unshift({ value, label: value, node: value });
    }
    return rows;
  }, [options, value]);

  const items = useMemo(() => {
    const custom = query.trim();
    if (
      !allowCustom ||
      custom === '' ||
      known.some((option) => option.value.toLowerCase() === custom.toLowerCase())
    ) {
      return known;
    }
    // `label` is the custom id, not the invitation to use it: the label is
    // what lands in the input once it is chosen.
    return [
      ...known,
      { value: custom, label: custom, node: t('manage:console.use_custom_value', { value: custom }) },
    ];
  }, [known, query, allowCustom, t]);

  // The `value` handed to Base UI is compared by identity when it decides a
  // selection changed, and a "change" rewrites the input text to the selected
  // label. A fresh object per render — from a list rebuilt on each keystroke
  // or a catalogue fetch landing — would therefore erase what the person is
  // typing, so the same logical selection must keep the same object.
  const selectedRef = useRef<ComboOption | null>(null);
  const selected = useMemo(() => {
    const found = known.find((option) => option.value === value) ?? null;
    const held = selectedRef.current;
    if (found && held && held.value === found.value && held.label === found.label) {
      return held;
    }
    selectedRef.current = found;
    return found;
  }, [known, value]);

  return (
    <ComboboxFieldGroup id={id}>
      <Combobox
        items={items}
        value={selected}
        disabled={disabled}
        onValueChange={(next: ComboOption | null) => onChange(next ? next.value : '')}
        onInputValueChange={setQuery}
        isItemEqualToValue={(a: ComboOption, b: ComboOption) => a.value === b.value}
        filter={comboMatches}
      >
        {/* The field is the search box itself — there is no separate trigger
            with a search line under it — so `searchPlaceholder` is what it says
            when it is empty, and `placeholder` is the fallback for a field that
            never named the search. */}
        {/* The chevron is a real, focusable button with no text. This field kit
            uses plain HTML rather than Base UI's Field context, so the explicit
            trigger label supplies its accessible name. */}
        <ComboboxInput
          id={id}
          disabled={disabled}
          placeholder={searchPlaceholder ?? placeholder ?? t('manage:console.select_placeholder')}
          triggerLabel={t('manage:console.open_options')}
          className="w-full"
        />
        <ComboboxOptions emptyLabel={emptyLabel ?? t('manage:console.no_search_results')} />
      </Combobox>
    </ComboboxFieldGroup>
  );
}

/** Searchable multi-select used for Agent MCP and Skill assignments. */
export function ConsoleSearchMultiSelect({
  id,
  value,
  onChange,
  options,
  placeholder,
  searchPlaceholder,
  emptyLabel,
  disabled,
}: {
  id?: string;
  value: string[];
  onChange: (value: string[]) => void;
  options: ConsoleSelectOption[];
  placeholder?: string;
  searchPlaceholder?: string;
  emptyLabel?: string;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const anchor = useComboboxAnchor();
  const items = useMemo(() => toComboOptions(options), [options]);
  // A selection the catalogue does not list is still assigned to the record, so
  // it keeps a chip of its own: an extension withdrawn from the gateway must
  // show as something the operator can see and unassign, not as a silent gap.
  const selected = useMemo(
    () =>
      value.map(
        (id_) =>
          items.find((option) => option.value === id_) ?? { value: id_, label: id_, node: id_ },
      ),
    [value, items],
  );

  return (
    <ComboboxFieldGroup id={id}>
      <Combobox
        multiple
        items={items}
        value={selected}
        disabled={disabled}
        onValueChange={(next: ComboOption[]) => onChange(next.map((option) => option.value))}
        isItemEqualToValue={(a: ComboOption, b: ComboOption) => a.value === b.value}
        filter={comboMatches}
      >
        {/* Chips rather than a count: the reader's next act is to remove one of
            these, and "3 selected" makes them open the list to find out which. */}
        <ComboboxChips ref={anchor} className="w-full">
          <ComboboxValue>
            {(chosen: ComboOption[]) => (
              <>
                {chosen.map((option) => (
                  <ComboboxChip key={option.value} aria-label={option.label}>
                    {option.node}
                  </ComboboxChip>
                ))}
                <ComboboxChipsInput
                  id={id}
                  disabled={disabled}
                  placeholder={
                    chosen.length === 0
                      ? (placeholder ?? t('manage:console.select_placeholder'))
                      : (searchPlaceholder ?? '')
                  }
                />
              </>
            )}
          </ComboboxValue>
        </ComboboxChips>
        <ComboboxOptions
          emptyLabel={emptyLabel ?? t('manage:console.no_search_results')}
          anchor={anchor}
        />
      </Combobox>
    </ComboboxFieldGroup>
  );
}

/** A closed set of choices, as the platform's native select — keyboard- and
 *  mobile-correct, and with no overlay to position. */
export function ConsoleSelect({
  id,
  value,
  onChange,
  options,
  placeholder,
  disabled,
  invalid,
}: {
  id?: string;
  value: string;
  onChange: (v: string) => void;
  options: ConsoleSelectOption[];
  placeholder?: string;
  disabled?: boolean;
  invalid?: boolean;
}) {
  const aria = useFieldAria();
  const { t } = useTranslation();
  // An unknown current value (open shape, e.g. a sandbox backend that isn't
  // in the enum) is surfaced as a real option so it's never silently lost.
  const known = options.some((o) => o.value === value);
  // Sans, like every control that offers a choice: mono means "you will type or
  // paste this" (§6), and a reader types none of a dropdown.
  return (
    <NativeSelect
      {...aria}
      className="w-full"
      id={id}
      value={value}
      disabled={disabled}
      aria-invalid={invalid || undefined}
      onChange={(e) => onChange(e.target.value)}
    >
      <NativeSelectOption value="" disabled>
        {placeholder ?? t('manage:console.select_placeholder')}
      </NativeSelectOption>
      {!known && value !== '' && <NativeSelectOption value={value}>{value}</NativeSelectOption>}
      {options.map((o) => (
        <NativeSelectOption key={o.value} value={o.value}>
          {optionText(o)}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}

/** A boolean as an on/off switch with an inline on/off legend, in console voice. */
export function ConsoleToggle({
  id,
  labelledBy,
  checked,
  onChange,
  disabled,
  onLabel,
  offLabel,
}: {
  /**
   * Lands on the kit switch's hidden checkbox, which is what a `<label for>`
   * can point at and what a click on the label flips. The visible switch is a
   * `<span role="switch">` and is named by `labelledBy` instead — `for` does
   * not name a span, so pointing it there would leave the control the reader
   * actually operates announced as nothing.
   */
  id?: string;
  /** The id of the text naming this field — see `id`. */
  labelledBy?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  onLabel?: React.ReactNode;
  offLabel?: React.ReactNode;
}) {
  const aria = useFieldAria();
  const { t } = useTranslation();
  const on = onLabel ?? t('manage:console.toggle_on');
  const off = offLabel ?? t('manage:console.toggle_off');
  return (
    <div className="flex h-8 items-center gap-2.5">
      <Switch
      {...aria}
        id={id}
        aria-labelledby={labelledBy}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(next) => onChange(next)}
      />
      {/* Sans: a legend is chrome naming a state, not something anyone types. */}
      <span className={cn('text-xs', checked ? 'text-foreground' : 'text-muted-foreground')}>
        {checked ? on : off}
      </span>
    </div>
  );
}

/** Editable string list — mono rows with a remove affordance + an add row.
 *  The kit's tag grammar, but writable. */
export function ConsoleListField({
  value,
  onChange,
  placeholder,
  example,
  disabled,
  labelledBy,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  example?: string;
  disabled?: boolean;
  /**
   * The id of the text naming this field. A list is several controls plus an
   * add button, with nothing for a `<label for>` to point at — and when the
   * list is empty there is no input at all. `for` names one form element;
   * naming a set is what `role="group"` + `aria-labelledby` is for.
   */
  labelledBy?: string;
}) {
  const { t } = useTranslation();
  const items = useMemo(() => (Array.isArray(value) ? value : []), [value]);

  // An empty row is a place to type, not a value. The rows on screen are held
  // here and only the filled ones are reported up, because a field's setter is
  // also what builds the payload it saves: a setter that drops blanks (as
  // `admins` does) removes the row Add just appended, leaving the button
  // apparently inert, while reporting the blank instead would write it.
  const [rows, setRows] = useState<string[]>(items);
  useEffect(() => {
    // Adopt a change made elsewhere — a revert, or the record arriving — and
    // leave a row being typed into alone otherwise.
    setRows((prev) =>
      JSON.stringify(prev.filter((r) => r.trim() !== '')) === JSON.stringify(items)
        ? prev
        : items,
    );
  }, [items]);

  const commit = (next: string[]) => {
    setRows(next);
    const filled = next.filter((r) => r.trim() !== '');
    if (JSON.stringify(filled) !== JSON.stringify(items)) onChange(filled);
  };
  const update = (i: number, v: string) => {
    const next = [...rows];
    next[i] = v;
    commit(next);
  };
  return (
    <div className="flex flex-col gap-1.5" role="group" aria-labelledby={labelledBy}>
      {rows.map((item, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Input
            type="text"
            className="font-mono"
            value={item}
            placeholder={example ?? placeholder ?? t('manage:console.list_add_placeholder')}
            disabled={disabled}
            spellCheck={false}
            onChange={(e) => update(i, e.target.value)}
            onKeyDown={(e) => acceptFieldExample(e, example, (v) => update(i, v))}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t('manage:console.list_remove_aria')}
            disabled={disabled}
            onClick={() => commit(rows.filter((_, j) => j !== i))}
            className="shrink-0 text-muted-foreground hover:text-destructive"
          >
            <X />
          </Button>
        </div>
      ))}
      {/* Dashed, because it adds a row rather than acting on the record: the
          border is the outline of the row that is not there yet. */}
      <Button
        type="button"
        variant="outline"
        size="xs"
        disabled={disabled}
        onClick={() => commit([...rows, ''])}
        className="w-fit border-dashed text-muted-foreground"
      >
        <Plus />
        {t('manage:console.list_add')}
      </Button>
    </div>
  );
}

/** Mono JSON escape hatch — the catch-all for structurally rich config blocks
 *  (key_value / object / object_list). Edits a text buffer; commits the parsed
 *  value on blur, keeping the last good value if the buffer is mid-edit invalid,
 *  and reports a parse error so the user knows the field won't save as typed. */
export function ConsoleJsonField({
  id,
  value,
  onChange,
  onValidityChange,
  placeholder,
  example,
  rows = 5,
  disabled,
}: {
  id?: string;
  value: unknown;
  onChange: (v: unknown) => void;
  /** Reports whether the current buffer parses — drives the field's error line. */
  onValidityChange?: (ok: boolean) => void;
  placeholder?: string;
  example?: string;
  rows?: number;
  disabled?: boolean;
}) {
  // The border says the same thing the error line says, at the place the caret
  const aria = useFieldAria();
  // already is. Held here rather than passed back down: the buffer is this
  // component's, and so is the answer to whether it parses.
  const [parses, setParses] = useState(true);
  const report = (ok: boolean) => {
    setParses(ok);
    onValidityChange?.(ok);
  };
  // Uncontrolled buffer (defaultValue) so an in-progress invalid edit isn't
  // stomped by a re-render of the parsed value.
  return (
    <Textarea
      {...aria}
      id={id}
      rows={rows}
      className="field-sizing-fixed resize-y font-mono text-11 leading-5"
      defaultValue={value == null ? '' : JSON.stringify(value, null, 2)}
      placeholder={example ?? placeholder}
      spellCheck={false}
      disabled={disabled}
      aria-invalid={!parses || undefined}
      onKeyDown={(e) => acceptFieldExample(e, example, (v) => {
        e.currentTarget.value = v;
        let parsed: unknown;
        try {
          parsed = JSON.parse(v);
        } catch {
          report(false);
          return;
        }
        report(true);
        onChange(parsed);
      })}
      onChange={(e) => {
        const raw = e.target.value.trim();
        if (raw === '') return report(true);
        try {
          JSON.parse(raw);
          report(true);
        } catch {
          report(false);
        }
      }}
      onBlur={(e) => {
        const raw = e.target.value.trim();
        if (raw === '') {
          report(true);
          return onChange(undefined);
        }
        try {
          onChange(JSON.parse(raw));
          report(true);
        } catch {
          report(false);
        }
      }}
    />
  );
}

/** Inline confirmation chip (mint, with a check) — the `Saved` / `Deleted` ack
 *  the footer flashes after a successful mutation. */
export function ConsoleSavedTag({ children }: { children?: React.ReactNode }) {
  const { t } = useTranslation();
  return (
    <span className="astra-enter inline-flex items-center gap-1.5 text-xs font-medium text-mint-fg">
      <Check className="size-3.5" />
      {children ?? t('common:saved')}
    </span>
  );
}
