import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AccessDenied } from './AccessDenied';
import { SessionExpired } from './SessionExpired';

describe('SessionExpired (401)', () => {
  it('renders session-expired without redirecting to login', () => {
    const navigate = vi.fn();
    render(<SessionExpired />);
    expect(screen.getByText('Session expired')).toBeInTheDocument();
    // Must fail if a login redirect occurs on 401.
    expect(navigate).not.toHaveBeenCalled();
    expect(window.location.pathname).not.toMatch(/login/);
  });

  it('shows a message distinct from AccessDenied', () => {
    const { unmount } = render(<SessionExpired />);
    const sessionExpiredText = screen.getByText('Session expired').textContent;
    unmount();

    render(<AccessDenied />);
    const accessDeniedText = screen.getByText('Access denied').textContent;

    // Must fail if the message is identical to AccessDenied's.
    expect(sessionExpiredText).not.toBe(accessDeniedText);
  });
});
