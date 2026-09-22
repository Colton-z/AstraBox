import { describe, expect, it } from 'vitest';

import type { EnvironmentConfig } from '@/types';

import { envEngineLabel } from './environmentConfig';

const environment = (engine_kind: string): EnvironmentConfig => ({
  name: `${engine_kind || 'blank'}-environment`,
  engine_kind,
});

describe('envEngineLabel', () => {
  it('renders installed and future engine identifiers as readable labels', () => {
    expect(envEngineLabel(environment('claude_code'))).toBe('Claude Code');
    expect(envEngineLabel(environment('deepseek_harness'))).toBe('DeepSeek Harness');
    expect(envEngineLabel(environment('codex'))).toBe('Codex');
    expect(envEngineLabel(environment('pi'))).toBe('Pi');
    expect(envEngineLabel(environment('future_engine'))).toBe('Future Engine');
    expect(envEngineLabel(environment(''))).toBe('—');
  });
});
