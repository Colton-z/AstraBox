/**
 * Console primitives — the operator-console grammar for the manage surface,
 * matching the design-system kit (dense hairline tables, mono eyebrow labels,
 * mono data values, status pills/dots, filter chips, designed empty states).
 *
 * A manage page adopts the grammar by composition: swap the page's shadcn
 * `<Table>` for {@link ConsoleTable} (declare columns), the title block for
 * {@link ConsolePageHeader}, the status `<Badge>` for {@link StatusPill}, the
 * `All statuses` dropdown for {@link FilterChips}, and the centered gray empty
 * text for {@link ConsoleEmptyState}.
 */
export { ConsolePageHeader, MetaLine } from './ConsolePageHeader';
export { ConsoleTable, type ConsoleColumn } from './ConsoleTable';
export { FilterChips, type FilterChipOption } from './FilterChips';
export { DateRangeFilter, type DateWindow } from './DateRangeFilter';
export { StatusPill, AstraMark, type PillTone } from './StatusPill';
export {
  ConsoleEmptyState,
  ConsoleTableNote,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  ConsoleRecordLoading,
} from './ConsoleEmptyState';
export { ConsoleSearch, NameCell, ConsoleToolbar } from './ConsoleControls';
export type {
  DrawerSectionSpec,
  DrawerFieldSpec,
  ConsoleStatus,
  EditFieldType,
  EditFieldSpec,
  EditSectionSpec,
} from './editFields';
export { splitAdvanced } from './editFields';
export { ConsoleCard, ConsoleFactRail, ConsoleFact } from './ConsoleCard';
export { ConsoleRecordPage } from './ConsoleRecordPage';
export { ConsoleCreatePage } from './ConsoleCreatePage';
export { ConsoleEditSections, type SectionCardProps } from './ConsoleEditSections';
export { sectionIsDirty, missingRequiredFields, revertFields } from './editDraft';
export { RecordCrumbProvider, useRecordCrumb } from './recordCrumb';
export { EditFieldControl } from './EditFieldControl';
export {
  ConsoleFieldRow,
  ConsoleTextField,
  ConsoleNumberField,
  ConsoleTextArea,
  ConsoleSelect,
  ConsoleSearchSelect,
  ConsoleSearchMultiSelect,
  ConsoleToggle,
  ConsoleListField,
  ConsoleJsonField,
  ConsoleSavedTag,
  type ConsoleSelectOption,
} from './ConsoleForm';
export { ConsoleDangerButton, type ConsoleDangerConfirm } from './ConsoleDangerButton';
export { formatDateTime, formatDateTimeSeconds } from './format';
