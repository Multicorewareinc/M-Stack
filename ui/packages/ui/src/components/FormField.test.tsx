import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FormField, Input } from './inputs';

describe('FormField', () => {
  it('associates label and error', () => {
    render(
      <FormField label="Organization name" htmlFor="org-name" error="Name is required">
        <Input />
      </FormField>,
    );
    const input = screen.getByLabelText('Organization name');
    // Must fail if the label/htmlFor/aria-describedby wiring is dropped.
    expect(input).toHaveAttribute('id', 'org-name');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    const describedBy = input.getAttribute('aria-describedby');
    expect(describedBy).toContain('org-name-error');
    expect(screen.getByText('Name is required')).toHaveAttribute('id', 'org-name-error');
  });

  it('omits error wiring when there is no error', () => {
    render(
      <FormField label="Slug" htmlFor="slug" hint="Lowercase only">
        <Input />
      </FormField>,
    );
    const input = screen.getByLabelText('Slug');
    expect(input).not.toHaveAttribute('aria-invalid');
    expect(input.getAttribute('aria-describedby')).toContain('slug-hint');
  });

  it('required field stays exact-match labelable (asterisk does not leak into the accessible name)', () => {
    render(
      <FormField label="Organization name" htmlFor="org-name" required>
        <Input />
      </FormField>,
    );
    // Must fail if the "*" ends up inside <label>, breaking
    // getByLabelText('Organization name') for every required field.
    expect(screen.getByLabelText('Organization name')).toHaveAttribute('id', 'org-name');
    expect(screen.getByText('*')).toBeInTheDocument();
  });
});
