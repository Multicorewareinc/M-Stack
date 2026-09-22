import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { EntityDrawer } from './overlays';

describe('EntityDrawer', () => {
  it('prompts for confirmation before closing a dirty drawer', async () => {
    const onOpenChange = vi.fn();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(
      <EntityDrawer open title="Edit Organization" dirty onOpenChange={onOpenChange}>
        <div>form</div>
      </EntityDrawer>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    // Must fail if the drawer closes immediately with no prompt.
    expect(confirmSpy).toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onOpenChange).toHaveBeenCalledWith(false);

    confirmSpy.mockRestore();
  });

  it('closes immediately when not dirty', async () => {
    const onOpenChange = vi.fn();
    const confirmSpy = vi.spyOn(window, 'confirm');
    render(
      <EntityDrawer open title="Edit Organization" onOpenChange={onOpenChange}>
        <div>form</div>
      </EntityDrawer>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    // Must fail if a prompt appears with nothing to discard.
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);

    confirmSpy.mockRestore();
  });

  it('renders loading and error states', () => {
    const { rerender } = render(
      <EntityDrawer open title="Create Organization" loading>
        <div>form</div>
      </EntityDrawer>,
    );
    // Loading state visible; form not shown yet.
    expect(screen.getByTestId('entity-drawer-loading')).toBeInTheDocument();
    expect(screen.queryByText('form')).toBeNull();

    rerender(
      <EntityDrawer open title="Create Organization" error="The organization could not be created.">
        <div>form</div>
      </EntityDrawer>,
    );
    // Must fail if the error content is not shown.
    expect(screen.getByRole('alert')).toHaveTextContent('The organization could not be created.');
    expect(screen.getByText('form')).toBeInTheDocument();
  });
});
