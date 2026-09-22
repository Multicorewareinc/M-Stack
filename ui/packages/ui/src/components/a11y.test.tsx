import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Button, Checkbox, FormField, Input } from './inputs';

describe('accessibility baseline', () => {
  it('interactive primitives are keyboard operable', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Button>Save</Button>
        <FormField label="Email" htmlFor="email">
          <Input />
        </FormField>
        <Checkbox aria-label="Accept" />
      </div>,
    );

    // Each control has an accessible name and is reachable by keyboard.
    const button = screen.getByRole('button', { name: 'Save' });
    const input = screen.getByLabelText('Email');
    const checkbox = screen.getByRole('checkbox', { name: 'Accept' });

    await user.tab();
    expect(button).toHaveFocus();
    await user.tab();
    // Must fail if a labeled control is not focusable in order.
    expect(input).toHaveFocus();
    await user.tab();
    expect(checkbox).toHaveFocus();
  });
});
