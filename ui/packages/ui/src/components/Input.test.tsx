import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Input } from './inputs';

describe('Input', () => {
  it('sets aria-invalid on error', () => {
    const { rerender } = render(<Input aria-label="Name" error />);
    // Must fail if aria-invalid is not set on error.
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true');

    rerender(<Input aria-label="Name" />);
    expect(screen.getByLabelText('Name')).not.toHaveAttribute('aria-invalid');
  });
});
