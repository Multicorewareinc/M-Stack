import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { RoleSelector, type SelectableRole } from './rbac';

const roles: SelectableRole[] = [
  { id: 'r1', name: 'Admin', description: 'Organization administrator' },
  { id: 'r2', name: 'Developer' },
  { id: 'r3', name: 'Viewer' },
];

describe('RoleSelector', () => {
  it('emits selected role ids', async () => {
    const onChange = vi.fn();
    render(<RoleSelector roles={roles} selectedIds={['r1']} onChange={onChange} />);
    await userEvent.click(screen.getByLabelText(/Developer/));
    // Must fail if the callback is not invoked with the new set.
    const emitted = onChange.mock.calls[0][0] as string[];
    expect(new Set(emitted)).toEqual(new Set(['r1', 'r2']));
  });

  it('a role with a description stays exact-match labelable by name alone', () => {
    render(<RoleSelector roles={roles} selectedIds={['r1']} onChange={vi.fn()} />);
    // Must fail if the description text leaks into the label's accessible
    // name (e.g. "AdminOrganization administrator"), breaking an exact-match
    // getByLabelText('Admin') query.
    expect(screen.getByLabelText('Admin')).toBeChecked();
    expect(screen.getByText('Organization administrator')).toBeInTheDocument();
  });
});
