// @vitest-environment jsdom
// A model endpoint that refuses the request does not fail the turn: the CLI
// retries ten times over about three minutes, announcing each attempt, so the
// reader needs the small public projection while the raw vendor event remains
// private.
import React from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/react';

import { PartRenderer } from './MessageParts';
import { ResultPartCard } from './ResultCards';

afterEach(cleanup);

function apiRetryPart(payload: Record<string, unknown>) {
  return {
    type: 'data-api-retry',
    data: payload,
  } as never;
}

function renderPart(part: never) {
  return render(<PartRenderer part={part} isActiveTurn isUser={false} />);
}

describe('the engine retrying a refused request', () => {
  const refused = {
    attempt: 3,
    max_retries: 10,
    error_status: 401,
    error: 'authentication_failed',
  };

  it("quotes the endpoint's own status and error name", () => {
    renderPart(apiRetryPart(refused));

    // Quoted, not restated: a deployment points at whatever gateway it likes,
    // so `401` and `authentication_failed` have to survive as spelled.
    const quoted = document.querySelector('[data-slot="verbatim"]');
    expect(quoted?.textContent).toBe('401 authentication_failed');
  });

  it('still shows the retry when the endpoint named no status', () => {
    // Nothing to quote is not nothing to say: the reader's question is whether
    // the turn is moving, and a retry answers it with or without a status.
    const { container } = renderPart(
      apiRetryPart({ attempt: 1, max_retries: 10 }),
    );

    expect(container.textContent?.trim()).not.toBe('');
    expect(container.querySelector('[data-slot="verbatim"]')).toBeNull();
  });

  it('does not render a generic raw event', () => {
    const { container } = renderPart(
      {
        type: 'data-raw-event',
        data: {
          event_type: 'vendor.internal',
          raw: { secret: 'must-not-render' },
        },
      } as never,
    );

    expect(container.innerHTML).toBe('');
    expect(container.textContent).not.toContain('must-not-render');
  });

  it('renders only the four retry fields', () => {
    const { container } = renderPart(apiRetryPart({
      ...refused,
      retry_delay_ms: 2125,
      raw: { secret: 'must-not-render' },
    }));

    expect(container.textContent).toContain('401 authentication_failed');
    expect(container.textContent).not.toContain('2125');
    expect(container.textContent).not.toContain('must-not-render');
  });
});

// The values match the public metric projection pinned by
// tests/claude_result_data_contract_test.py.
describe('the line that closes a turn', () => {
  const settled = {
    duration_ms: 4210,
    total_cost_usd: 0.0137,
    num_turns: 2,
    usage: { input_tokens: 1204, output_tokens: 88 },
    stop_reason: 'end_turn',
  };

  it('prints the meter a reader can check the turn against', () => {
    const { container } = render(<ResultPartCard data={settled} />);
    expect(container.textContent).toContain('4.2s');
    expect(container.textContent).toContain('$0.0137');
  });

  it('says nothing when the engine reported nothing', () => {
    const { container } = render(<ResultPartCard data={{}} />);
    expect(container.innerHTML).toBe('');
  });

  it('marks a stop the reader has to act on', () => {
    const { container } = render(<ResultPartCard data={{ ...settled, stop_reason: 'max_tokens' }} />);
    expect(container.querySelector('.text-citrine-fg')).not.toBeNull();
  });

  it('leaves a normal stop unmarked', () => {
    const { container } = render(<ResultPartCard data={settled} />);
    expect(container.querySelector('.text-citrine-fg')).toBeNull();
  });
});
