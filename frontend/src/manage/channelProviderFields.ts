import type { ChannelFieldDescriptor } from '@/types';

import type { EditFieldSpec, EditFieldType } from './console';

export type ChannelDraftSection = 'channel_config' | 'credentials';

function sectionValue(
  draft: Record<string, unknown>,
  section: ChannelDraftSection,
): Record<string, unknown> {
  const value = draft[section];
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function controlType(field: ChannelFieldDescriptor): EditFieldType {
  if (field.secret) return 'password';
  if (field.kind === 'number') return 'number';
  if (field.kind === 'boolean') return 'toggle';
  if (field.kind === 'select') return 'select';
  return 'text';
}

export function channelFieldValue(
  draft: Record<string, unknown>,
  section: ChannelDraftSection,
  field: ChannelFieldDescriptor,
): unknown {
  const values = sectionValue(draft, section);
  return Object.hasOwn(values, field.key) ? values[field.key] : field.default;
}

export function channelEditField(
  field: ChannelFieldDescriptor,
  section: ChannelDraftSection,
): EditFieldSpec {
  return {
    key: `${section}:${field.key}`,
    label: field.label,
    type: controlType(field),
    required: field.required,
    idTag: field.key,
    options: field.options.map((value) => ({ value, label: value })),
    placeholder: field.placeholder,
    help: field.help,
    get: (draft) => channelFieldValue(draft, section, field),
    set: (draft, value) => ({
      ...draft,
      [section]: { ...sectionValue(draft, section), [field.key]: value },
    }),
  };
}

export function channelFieldValues(
  draft: Record<string, unknown>,
  section: ChannelDraftSection,
  fields: ChannelFieldDescriptor[],
): Record<string, unknown> {
  const output: Record<string, unknown> = {};
  for (const field of fields) {
    const value = channelFieldValue(draft, section, field);
    if (value !== undefined && value !== null && value !== '') output[field.key] = value;
  }
  return output;
}

export function firstMissingChannelField(
  draft: Record<string, unknown>,
  section: ChannelDraftSection,
  fields: ChannelFieldDescriptor[],
): ChannelFieldDescriptor | undefined {
  return fields.find((field) => {
    if (!field.required) return false;
    const value = channelFieldValue(draft, section, field);
    return value === undefined || value === null || value === '';
  });
}

export function hasChannelFieldValue(
  draft: Record<string, unknown>,
  section: ChannelDraftSection,
  fields: ChannelFieldDescriptor[],
): boolean {
  return fields.some((field) => {
    const value = channelFieldValue(draft, section, field);
    return value !== undefined && value !== null && value !== '';
  });
}
