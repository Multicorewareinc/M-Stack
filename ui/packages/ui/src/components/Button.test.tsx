import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Button } from './inputs';

describe('Button', () => {
  it('renders loading and disabled states', async () => {
    const onClick = vi.fn();
    const { rerender } = render(<Button loading onClick={onClick}>Save</Button>);
    const btn = screen.getByRole('button', { name: /save/i });
    // Must fail if a loading button stays interactive.
    expect(btn).toHaveAttribute('aria-busy', 'true');
    expect(btn).toBeDisabled();
    await userEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();

    rerender(<Button disabled onClick={onClick}>Save</Button>);
    expect(screen.getByRole('button', { name: /save/i })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: /save/i }));
    expect(onClick).not.toHaveBeenCalled();
  });

  it('is clickable when idle', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    await userEvent.click(screen.getByRole('button', { name: /go/i }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
