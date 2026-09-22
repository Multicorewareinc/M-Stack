import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { PermissionMatrix, type MatrixPermission } from './rbac';

const permissions: MatrixPermission[] = [
  { id: 'p1', resource: 'users', action: 'read' },
  { id: 'p2', resource: 'users', action: 'create' },
  { id: 'p3', resource: 'roles', action: 'read' },
];

describe('PermissionMatrix', () => {
  it('groups by resource and emits selected ids', async () => {
    const onChange = vi.fn();
    render(<PermissionMatrix permissions={permissions} selectedIds={['p1']} onChange={onChange} />);

    // Grouped by resource.
    expect(screen.getByRole('heading', { name: 'users' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'roles' })).toBeInTheDocument();

    // Toggle "create" (p2) on; existing p1 remains.
    await userEvent.click(screen.getByLabelText('create'));
    // Must fail if the emitted payload differs from the toggled selection.
    const emitted = onChange.mock.calls[0][0] as string[];
    expect(new Set(emitted)).toEqual(new Set(['p1', 'p2']));
  });

  it('removes a permission when toggled off', async () => {
    const onChange = vi.fn();
    render(<PermissionMatrix permissions={permissions} selectedIds={['p1', 'p2']} onChange={onChange} />);
    // The first "read" checkbox is users.read (p1); toggling it removes p1.
    await userEvent.click(screen.getAllByLabelText('read')[0]);
    const emitted = onChange.mock.calls[0][0] as string[];
    expect(emitted).not.toContain('p1');
  });
});
