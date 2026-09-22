import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { DataTable, type Column } from './table';

interface Org {
  id: string;
  name: string;
}

const columns: Column<Org>[] = [
  { key: 'name', header: 'Organization', sortable: true, accessor: (r) => r.name },
];
const rows: Org[] = [
  { id: 'o1', name: 'Acme Corp' },
  { id: 'o2', name: 'Example Inc' },
];

describe('DataTable', () => {
  it('sorts on header activation', async () => {
    const onSortChange = vi.fn();
    const { rerender } = render(
      <DataTable columns={columns} rows={rows} getRowId={(r) => r.id} onSortChange={onSortChange} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /organization/i }));
    // Must fail if activation does not emit sort.
    expect(onSortChange).toHaveBeenCalledWith('name', 'asc');

    rerender(
      <DataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        sort={{ key: 'name', direction: 'asc' }}
        onSortChange={onSortChange}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /organization/i }));
    expect(onSortChange).toHaveBeenLastCalledWith('name', 'desc');
  });

  it('renders loading, empty, and error distinctly', () => {
    const { rerender } = render(
      <DataTable columns={columns} rows={[]} getRowId={(r) => r.id} loading />,
    );
    expect(screen.getByTestId('datatable-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('datatable-empty')).toBeNull();

    rerender(<DataTable columns={columns} rows={[]} getRowId={(r) => r.id} empty={{ title: 'No organizations yet' }} />);
    expect(screen.getByTestId('datatable-empty')).toBeInTheDocument();
    expect(screen.getByText('No organizations yet')).toBeInTheDocument();

    rerender(<DataTable columns={columns} rows={[]} getRowId={(r) => r.id} error="Boom" />);
    // Must fail if empty and error render identically.
    expect(screen.getByTestId('datatable-error')).toBeInTheDocument();
    expect(screen.queryByTestId('datatable-empty')).toBeNull();
  });

  it('row action menu invokes callbacks', async () => {
    const onEdit = vi.fn();
    render(
      <DataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        rowActions={() => [{ label: 'Edit', onClick: onEdit }]}
      />,
    );
    const trigger = screen.getAllByRole('button', { name: 'Row actions' })[0];
    await userEvent.click(trigger);
    const menu = await screen.findByRole('menu');
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Edit' }));
    // Must fail if an action fires without its row payload.
    expect(onEdit).toHaveBeenCalledWith(rows[0]);
  });
});
