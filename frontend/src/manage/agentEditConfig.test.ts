// The existing engine bag is edited as JSON without rewriting its values.
import { describe, expect, it, beforeAll } from 'vitest';

import i18n from '../i18n';
import type { FormSchema, ModelAccount } from '../types';
import { splitAdvanced } from './console';
import { agentPayloadForProfile, buildAgentEditSections } from './agentEditConfig';

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
  runtime_profiles: [],
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

describe('the ChatGPT account beside the model', () => {
  const PLAN_SCHEMA: FormSchema = {
    version: 1,
    groups: [{ id: 'model' }],
    fields: [
      { key: 'model', type: 'string', group: 'model', required: true },
      { key: 'system', type: 'text', group: 'model' },
      { key: 'model_account_id', type: 'string', group: 'model', advanced: true },
    ],
  };
  const account = (over: Partial<ModelAccount>): ModelAccount => ({
    account_id: 'mact_work',
    vendor: 'chatgpt_plan',
    label: 'Work',
    email: 'me@example.com',
    status: 'active',
    is_default: true,
    scopes: [],
    access_expires_at: null,
    last_error_code: null,
    last_error_at: null,
    created_at: '2026-11-14T00:00:00Z',
    updated_at: '2026-11-14T00:00:00Z',
    ...over,
  });
  // The model group's own card; access control always follows it.
  const modelKeys = (sections: ReturnType<typeof buildAgentEditSections>['sections']) =>
    sections[0].fields.map((f) => f.key);

  it('stands after the model on a ChatGPT plan profile, unfolded, with the default account first', () => {
    const { sections, advancedKeys } = buildAgentEditSections(PLAN_SCHEMA, {}, {
      nameEditable: true,
      chatgptPlan: {
        accounts: [
          account({}),
          account({ account_id: 'mact_home', label: 'Home', email: null, is_default: false, status: 'sign_in_required' }),
        ],
      },
    });
    expect(modelKeys(sections)).toEqual(['model', 'model_account_id', 'system']);
    expect(advancedKeys.has('model_account_id')).toBe(false);
    const field = sections.flatMap((s) => s.fields).find((f) => f.key === 'model_account_id')!;
    expect(field.type).toBe('select');
    expect(field.options).toEqual([
      { value: '', label: 'My default account (Work)' },
      { value: 'mact_work', label: 'Work · me@example.com' },
      { value: 'mact_home', label: 'Home — sign in again' },
    ]);
    // The default is stored as null, so choosing it on a saved Agent clears the
    // account chosen before instead of leaving it in place.
    expect(field.set({ model_account_id: 'mact_work' }, '')).toEqual({ model_account_id: null });
    expect(field.get({ model_account_id: null })).toBe('');
    expect(field.set({}, 'mact_home')).toEqual({ model_account_id: 'mact_home' });
  });

  it('is not offered, and not sent, where it would mean nothing', () => {
    // Another profile, and someone else's Agent on a ChatGPT plan profile.
    for (const chatgptPlan of [undefined, {}]) {
      const { sections } = buildAgentEditSections(PLAN_SCHEMA, {}, { nameEditable: false, chatgptPlan });
      expect(modelKeys(sections)).toEqual(['model', 'system']);
    }
    const draft = { name: 'helper', model: 'gpt-6.1-sol', model_account_id: 'mact_work' };
    expect(agentPayloadForProfile(draft, { name: 'gateway', endpoint_provider: 'litellm' }))
      .toEqual({ name: 'helper', model: 'gpt-6.1-sol' });
    expect(agentPayloadForProfile(draft, undefined)).toEqual({ name: 'helper', model: 'gpt-6.1-sol' });
    expect(agentPayloadForProfile(draft, { name: 'plan', endpoint_provider: 'chatgpt_plan' })).toEqual(draft);
  });
});
