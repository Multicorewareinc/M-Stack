import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AccessDenied } from './AccessDenied';

describe('AccessDenied (403)', () => {
  it('renders access denied without redirecting to login', () => {
    const navigate = vi.fn();
    render(<AccessDenied />);
    expect(screen.getByText('Access denied')).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
    expect(window.location.pathname).not.toMatch(/login/);
  });
});
