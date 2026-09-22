import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Toast } from './feedback';

describe('Toast', () => {
  it('renders all four variants with a11y role', () => {
    for (const variant of ['success', 'warning', 'info'] as const) {
      const { unmount } = render(<Toast variant={variant} title={`${variant} title`} />);
      expect(screen.getByRole('status')).toHaveTextContent(`${variant} title`);
      unmount();
    }
    render(<Toast variant="error" title="Unable to save" />);
    // Must fail if error toasts lack an assertive/alert role.
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Unable to save');
    expect(alert).toHaveAttribute('aria-live', 'assertive');
  });
});
