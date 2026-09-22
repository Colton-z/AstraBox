// The existing engine bag is edited as JSON without rewriting its values.
import { describe, expect, it, beforeAll } from 'vitest';

import i18n from '../i18n';
import type { FormSchema } from '../types';
import { splitAdvanced } from './console';
import { buildAgentEditSections } from './agentEditConfig';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

const SCHEMA: FormSchema = {
  version: 1,
  groups: [{ id: 'model' }],
  fields: [
    { key: 'model', type: 'string', group: 'model', required: true },
    { key: 'engine_options', type: 'object', group: 'model', advanced: true },
  ],
};

const opts = (engineOptionsSchema?: FormSchema['fields']) => ({
  nameEditable: true,
  environments: [],
  models: [],
  engineOptionsSchema,
});

const fieldKeys = (sections: ReturnType<typeof buildAgentEditSections>['sections']) =>
  sections.flatMap((s) => s.fields.map((f) => f.key));

describe('engine_options rendering follows the engine declaration', () => {
  it('renders native blocks with their target help and preserves arbitrary inner JSON', () => {
    const { sections } = buildAgentEditSections(SCHEMA, {}, opts([
      { key: 'settings', type: 'object', label: 'Native settings', help: 'Replaces settings.json root' },
      { key: 'turn_start', type: 'object', label: 'Turn parameters' },
    ]));
    const keys = fieldKeys(sections);
    expect(keys).not.toContain('engine_options');
    expect(keys).toContain('engine_options.settings');
    expect(keys).toContain('engine_options.turn_start');
    const preset = sections
      .flatMap((s) => s.fields)
      .find((f) => f.key === 'engine_options.settings');
    expect(preset?.type).toBe('json');
    expect(preset?.help).toContain('Replaces settings.json root');
    const bag = { future_vendor_option: { nested: [false, 5, '原样'] } };
    const written = preset!.set({}, bag);
    expect(written).toEqual({ engine_options: { settings: bag } });
    expect(preset!.get(written)).toEqual(bag);
  });

  it('renders no engine_options control when the engine declares nothing', () => {
    const { sections } = buildAgentEditSections(SCHEMA, {}, opts(undefined));
    expect(fieldKeys(sections).some((k) => k.startsWith('engine_options'))).toBe(false);
  });

  it('keeps an explicit empty object so saving clears previous overrides', () => {
    const { sections } = buildAgentEditSections(SCHEMA, {}, opts([
      { key: 'settings', type: 'object' },
    ]));
    const field = sections.flatMap((section) => section.fields)
      .find((entry) => entry.key === 'engine_options.settings')!;
    expect(field.set({ engine_options: { settings: { defaultThinkingLevel: 'high' }, other: {} } }, {}))
      .toEqual({ engine_options: { settings: {}, other: {} } });
    expect(field.set({}, undefined)).toEqual({});
    expect(field.set({ engine_options: { settings: { defaultThinkingLevel: 'high' } } }, undefined))
      .toEqual({ engine_options: {} });
  });

  it('keeps the schema advanced setting on the JSON control', () => {
    const { sections, advancedKeys } = buildAgentEditSections(SCHEMA, {}, opts([
      { key: 'settings', type: 'object' },
    ]));
    expect(advancedKeys.has('engine_options.settings')).toBe(true);
    const { essential, advanced, advancedCount } = splitAdvanced(sections, advancedKeys);
    expect(advancedCount).toBe(1);
    expect(advanced.flatMap((s) => s.fields).map((f) => f.key)).toEqual([
      'engine_options.settings',
    ]);
    expect(essential.flatMap((s) => s.fields).some((f) => f.key.startsWith('engine_options')))
      .toBe(false);
  });
});
