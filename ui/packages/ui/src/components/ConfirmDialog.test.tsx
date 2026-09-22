import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ConfirmDialog } from './overlays';

describe('ConfirmDialog', () => {
  it('destructive confirm shows loading and blocks double submit', async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        title="Delete organization?"
        description="This action cannot be undone."
        confirmLabel="Delete Organization"
        destructive
        loading
        onConfirm={onConfirm}
      />,
    );
    const confirm = await screen.findByRole('button', { name: /delete organization/i });
    // Must fail if the button can be clicked while pending.
    expect(confirm).toHaveAttribute('aria-busy', 'true');
    expect(confirm).toBeDisabled();
    await userEvent.click(confirm);
    await userEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('invokes onConfirm once when idle', async () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog open title="Suspend?" confirmLabel="Suspend" onConfirm={onConfirm} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Suspend' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
