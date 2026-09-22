import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatusBadge } from './status';

describe('StatusBadge', () => {
  it('renders known statuses with accessible label', () => {
    for (const [status, label] of [
      ['active', 'Active'],
      ['provisioning', 'Provisioning'],
      ['failed', 'Failed'],
      ['suspended', 'Suspended'],
    ] as const) {
      const { unmount } = render(<StatusBadge status={status} />);
      const el = screen.getByRole('status', { name: `Status: ${label}` });
      expect(el).toHaveTextContent(label);
      // Non-color indicator: an icon carrying the status shape is present.
      // Must fail if status is conveyed by color only (no icon marker).
      expect(el.querySelector(`[data-icon="${status}"]`)).not.toBeNull();
      unmount();
    }
  });

  it('unknown status falls back', () => {
    render(<StatusBadge status="WEIRD_STATE" />);
    const el = screen.getByRole('status', { name: 'Status: Unknown' });
    // Must fail if it renders the raw value or crashes.
    expect(el).toHaveTextContent('Unknown');
    expect(el).not.toHaveTextContent('WEIRD_STATE');
    expect(el.querySelector('[data-icon="unknown"]')).not.toBeNull();
  });
});
