import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { EmptyState } from './feedback';

describe('EmptyState', () => {
  it('renders title, description, action', async () => {
    const onClick = vi.fn();
    render(
      <EmptyState
        title="No organizations yet"
        description="Create your first organization to get started."
        action={{ label: 'Create Organization', onClick }}
      />,
    );
    expect(screen.getByText('No organizations yet')).toBeInTheDocument();
    expect(screen.getByText(/create your first organization/i)).toBeInTheDocument();
    const btn = screen.getByRole('button', { name: 'Create Organization' });
    await userEvent.click(btn);
    // Must fail if the primary action is not rendered/operable.
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('omits the action when none is given', () => {
    render(<EmptyState title="No results found" />);
    expect(screen.queryByRole('button')).toBeNull();
  });
});
