// The create form starts from the values the server would store.
import { describe, expect, it } from 'vitest';

import type { FormSchema } from '../types';
import { buildAgentDraft } from './agentConfig';

const schemaWithCapacityDefault = (value: number | undefined): FormSchema => ({
  version: 1,
  groups: [{ id: 'runtime' }],
  fields: [
    { key: 'model', type: 'string', group: 'model', required: true },
    {
      key: 'prepared_capacity',
      type: 'integer',
      group: 'runtime',
      advanced: true,
      ...(value === undefined ? {} : { default: value }),
    },
  ],
});

describe('buildAgentDraft', () => {
  it('shows one prepared unit when this deployment creates Agents prewarmed', () => {
    expect(buildAgentDraft(schemaWithCapacityDefault(1)).prepared_capacity).toBe(1);
  });

  it('shows no prepared capacity when this deployment cannot prewarm', () => {
    expect(buildAgentDraft(schemaWithCapacityDefault(0)).prepared_capacity).toBe(0);
  });

  it('adds nothing for a field the schema serves no default for', () => {
    expect(buildAgentDraft(schemaWithCapacityDefault(undefined))).toEqual({
      name: '',
      enabled: true,
    });
  });
});
