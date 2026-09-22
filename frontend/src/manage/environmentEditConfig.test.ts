import { beforeAll, describe, expect, it } from 'vitest';

import i18n from '@/i18n';
import type { FormSchema } from '@/types';

import { buildEnvEditSections } from './environmentEditConfig';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

// The runtime group as astrabox/core/service/orchestrator/environment_schema.py
// declares it: networking is a closed product shape, while provider_access
// carries a credential.
const SCHEMA: FormSchema = {
  version: 1,
  groups: [{ id: 'runtime', label: 'Runtime' }],
  fields: [
    {
      key: 'networking',
      type: 'object',
      group: 'runtime',
      complex: true,
      item_schema: [
        { key: 'type', type: 'enum', enum: ['unrestricted', 'limited'] },
        { key: 'allowed_hosts', type: 'string_list' },
        { key: 'allow_mcp_servers', type: 'boolean' },
      ],
    },
    {
      key: 'idle_action',
      type: 'enum',
      group: 'runtime',
      advanced: true,
      // What the server actually serves: real actions only. An environment
      // states its own, so there is no empty value deferring to the install.
      enum: ['terminate', 'pause'],
    },
    {
      key: 'sandbox_tenancy',
      type: 'enum',
      group: 'runtime',
      advanced: true,
      enum: ['conversation', 'agent'],
    },
    {
      key: 'sandbox_backend',
      type: 'enum',
      group: 'runtime',
      advanced: true,
      enum: ['open_sandbox'],
    },
    {
      key: 'provider_access',
      type: 'object',
      group: 'runtime',
      complex: true,
      item_schema: [
        { key: 'base_url', type: 'string' },
        { key: 'api_key', type: 'string', advanced: true },
      ],
    },
  ],
} as unknown as FormSchema;

function fieldsOf(opts: { idleActions?: string[] | null }) {
  const { sections } = buildEnvEditSections(SCHEMA, {
    nameEditable: false,
    ...opts,
  });
  return { fields: sections.flatMap((s) => s.fields) };
}

describe('the environment form’s networking section', () => {
  it('renders the AstraBox contract as controls rather than provider JSON', () => {
    const { fields } = fieldsOf({});
    const networking = fields.filter((field) => field.key.startsWith('networking.'));

    expect(networking.map((field) => field.key)).toEqual([
      'networking.type',
      'networking.allowed_hosts',
      'networking.allow_mcp_servers',
    ]);
    expect(networking.map((field) => field.type)).toEqual(['select', 'list', 'toggle']);
    expect(networking[0].options?.map((option) => option.value)).toEqual([
      'unrestricted',
      'limited',
    ]);
    expect(networking[0].options?.map((option) => option.label)).toEqual([
      'Unrestricted',
      'Limited',
    ]);
  });

  it('writes only the provider-neutral networking document', () => {
    const { fields } = fieldsOf({});
    const type = fields.find((field) => field.key === 'networking.type')!;
    const hosts = fields.find((field) => field.key === 'networking.allowed_hosts')!;
    const mcp = fields.find((field) => field.key === 'networking.allow_mcp_servers')!;

    const draft = mcp.set(
      hosts.set(type.set({}, 'limited'), ['api.example.com']),
      true,
    );
    expect(draft).toEqual({
      networking: {
        type: 'limited',
        allowed_hosts: ['api.example.com'],
        allow_mcp_servers: true,
      },
    });
    expect(JSON.stringify(draft)).not.toContain('defaultAction');
  });
});

describe('the environment form’s structured fields', () => {
  it('inlines nothing that would round-trip a masked secret', () => {
    const { fields } = fieldsOf({});
    // provider_access.api_key is masked in read views; a text input over a mask
    // would save the mask over the real credential on the next Save.
    expect(fields.some((f) => f.key.startsWith('provider_access.'))).toBe(false);
    expect(fields.find((f) => f.key === 'provider_access')?.type).toBe('json');
  });

  it('offers every field outright, advanced or not', () => {
    // An admin surface. Its reader is choosing sandbox backends and model
    // gateways, so nothing here is held back behind a disclosure — and the
    // builder does not even compute which fields would be. The Agent create
    // form is where folding belongs, and it has its own coverage.
    const { fields } = fieldsOf({});
    expect(fields.some((f) => f.key === 'provider_access')).toBe(true);
    expect(
      (buildEnvEditSections(SCHEMA, { nameEditable: false }) as Record<string, unknown>)
        .advancedKeys,
    ).toBeUndefined();
  });
});


describe('the environment form’s idle action', () => {
  it('offers every action while the backend has not answered', () => {
    // A probe that has not landed, or failed, must not narrow anything: the
    // write path refuses an unsupported action anyway, so guessing here would
    // only hide a valid choice.
    const { fields } = fieldsOf({});
    const idle = fields.find((f) => f.key === 'idle_action');
    expect(idle?.options?.map((o) => o.value)).toEqual(['terminate', 'pause']);
  });

  it('drops an action the backend cannot carry out', () => {
    const { fields } = fieldsOf({ idleActions: ['terminate'] });
    const idle = fields.find((f) => f.key === 'idle_action');
    expect(idle?.options?.map((o) => o.value)).toEqual(['terminate']);
  });

  it('stays editable when pausing is unavailable, because terminate still is', () => {
    // This field keeps a valid choice to make, so disabling it would lock an
    // operator out of the action their deployment can carry out.
    const { fields } = fieldsOf({ idleActions: ['terminate'] });
    const idle = fields.find((f) => f.key === 'idle_action');
    expect(idle?.disabled).toBeFalsy();
    expect(idle?.editable).toBe(true);
  });

  it('offers no empty choice, because every environment states its own action', () => {
    // Nothing here may mean "the installation decides". The write path settles
    // a value before the document is stored, and the sweep reads only that, so
    // an empty option would offer a state no stored environment can be in.
    const { fields } = fieldsOf({ idleActions: ['pause'] });
    const idle = fields.find((f) => f.key === 'idle_action');
    expect(idle?.options?.map((o) => o.value)).toEqual(['pause']);
    expect(idle?.options?.some((o) => o.value === '')).toBe(false);
  });
});

describe('what an enum’s options are called on screen', () => {
  it('spells sandbox tenancy out, instead of showing its wire values', () => {
    // The wire values are `conversation` / `agent`. Shown raw they read as
    // internals rather than as the choice they stand for. The value saved is
    // unchanged; only the text is copy.
    const { fields } = fieldsOf({});
    const tenancy = fields.find((f) => f.key === 'sandbox_tenancy')!;
    expect(tenancy.options?.map((o) => o.value)).toEqual(['conversation', 'agent']);
    for (const option of tenancy.options ?? []) {
      expect(option.label).not.toBe(option.value);
      expect(option.label).toBe(
        i18n.t(`manage:env_form.fields.sandbox_tenancy.options.${option.value}`),
      );
    }
  });

  it('leaves a value that IS the operator’s own name alone', () => {
    // A backend is `open_sandbox` in the docs, the logs and the env var.
    // Translating it here would make three places disagree about its name.
    const { fields } = fieldsOf({});
    const backend = fields.find((f) => f.key === 'sandbox_backend')!;
    expect(backend.options).toEqual([{ value: 'open_sandbox', label: 'open_sandbox' }]);
  });
});
