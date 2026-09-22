import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Pagination } from './table';

describe('Pagination', () => {
  it('emits page changes with range summary', async () => {
    const onPageChange = vi.fn();
    render(<Pagination page={1} pageSize={25} total={981} onPageChange={onPageChange} />);
    // Range summary present.
    expect(screen.getByText('Rows 1–25 of 981')).toBeInTheDocument();
    // Previous disabled on first page.
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    // Must fail if the page callback is not emitted.
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it('disables Next on the last page', () => {
    const onPageChange = vi.fn();
    render(<Pagination page={40} pageSize={25} total={981} onPageChange={onPageChange} />);
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });
});
