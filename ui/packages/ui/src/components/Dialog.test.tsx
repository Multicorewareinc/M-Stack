import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Button } from './inputs';
import { Dialog } from './overlays';

describe('Dialog', () => {
  it('traps focus and closes on Escape', async () => {
    const user = userEvent.setup();
    render(
      <Dialog trigger={<Button>Open</Button>} title="Delete organization?" description="This cannot be undone.">
        <p>Body</p>
      </Dialog>,
    );
    const trigger = screen.getByRole('button', { name: 'Open' });
    await user.click(trigger);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();
    // Focus is moved into the dialog while open.
    expect(dialog.contains(document.activeElement)).toBe(true);

    await user.keyboard('{Escape}');
    // Must fail if the dialog does not close on Escape or focus is not restored.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(trigger).toHaveFocus();
  });
});
