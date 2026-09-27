// The create form starts from the values the server would store.
import { describe, expect, it } from 'vitest';

import type { FormSchema } from '../types';
import { buildAgentDraft } from './agentConfig';

const schemaWithPrewarmDefault = (value: boolean | undefined): FormSchema => ({
  version: 1,
  groups: [{ id: 'runtime' }],
  fields: [
    { key: 'model', type: 'string', group: 'model', required: true },
    {
      key: 'prewarm_enabled',
      type: 'boolean',
      group: 'runtime',
      advanced: true,
      ...(value === undefined ? {} : { default: value }),
    },
  ],
});

describe('buildAgentDraft', () => {
  it('shows prewarming on when this deployment creates Agents prewarmed', () => {
    expect(buildAgentDraft(schemaWithPrewarmDefault(true)).prewarm_enabled).toBe(true);
  });

  it('shows prewarming off when this deployment cannot prewarm', () => {
    expect(buildAgentDraft(schemaWithPrewarmDefault(false)).prewarm_enabled).toBe(false);
  });

  it('adds nothing for a field the schema serves no default for', () => {
    expect(buildAgentDraft(schemaWithPrewarmDefault(undefined))).toEqual({
      name: '',
      enabled: true,
    });
  });
});
