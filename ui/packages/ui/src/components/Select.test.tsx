import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Select } from './select';

const options = [
  { value: 'plan_free', label: 'Free' },
  { value: 'plan_pro', label: 'Pro' },
];

describe('Select', () => {
  it('selects an option via userEvent.selectOptions and emits the value', async () => {
    const onValueChange = vi.fn();
    render(<Select aria-label="Plan" options={options} onValueChange={onValueChange} />);

    const select = screen.getByLabelText('Plan');
    await userEvent.selectOptions(select, 'Pro');

    // Must fail if selecting an option does not emit its value.
    expect(onValueChange).toHaveBeenCalledWith('plan_pro');
  });

  it('renders the placeholder when no value is selected', () => {
    render(<Select aria-label="Plan" options={options} placeholder="Choose a plan" />);
    // The placeholder <option> is `hidden` (disabled placeholder), so it's
    // excluded from the accessibility tree by default — query by text instead.
    expect(screen.getByText('Choose a plan')).toBeInTheDocument();
  });
});
