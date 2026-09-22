import { ArrowDown, ArrowUp, ChevronsUpDown, MoreHorizontal, Search } from 'lucide-react';
import * as React from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../styles/cn';
import { Button, IconButton, Input } from './inputs';
import { EmptyState } from './feedback';
import { Skeleton } from './status';

/* ------------------------------------------------------------ SearchInput */

export interface SearchInputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  onValueChange?: (value: string) => void;
}

export function SearchInput({ className, onValueChange, onChange, ...props }: SearchInputProps) {
  return (
    <div className={cn('relative', className)}>
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-400" aria-hidden="true" />
      <Input
        type="search"
        className="pl-8"
        onChange={(e) => {
          onChange?.(e);
          onValueChange?.(e.target.value);
        }}
        {...props}
      />
    </div>
  );
}

/* -------------------------------------------------------------- FilterBar */

export function FilterBar({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn('flex flex-wrap items-center gap-2', className)}>{children}</div>;
}

/* ------------------------------------------------------------- Pagination */

export interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  className?: string;
}

export function Pagination({ page, pageSize, total, onPageChange, className }: PaginationProps) {
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className={cn('flex items-center justify-between gap-4 text-secondary text-neutral-500', className)}>
      <span>{`Rows ${from}–${to} of ${total}`}</span>
      <div className="flex items-center gap-2">
        <Button variant="secondary" size="sm" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
          Previous
        </Button>
        <Button variant="secondary" size="sm" disabled={page >= lastPage} onClick={() => onPageChange(page + 1)}>
          Next
        </Button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------- Table core */

export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn('w-full border-collapse text-body', className)} {...props} />
    </div>
  );
}

/* --------------------------------------------------------- RowActionsMenu */

interface RowActionsMenuProps<T> {
  row: T;
  actions: RowAction<T>[];
}

interface MenuPlacement {
  top?: number;
  bottom?: number;
  left: number;
  maxHeight: number;
}

/**
 * Compact overflow menu for a table row. A small controlled disclosure
 * (button + role="menu" list) rather than a full library menu — but the
 * panel itself is portalled to document.body and positioned with fixed
 * coordinates (not rendered inline), because the table wrapper scrolls
 * horizontally (`overflow-x-auto` in Table above), and per the CSS spec
 * setting overflow-x to anything but visible forces overflow-y to compute
 * as auto too — so an inline absolutely-positioned menu gets clipped by
 * that ancestor's box instead of the viewport. Closes on outside click,
 * Escape, and scroll/resize (its fixed coordinates would otherwise go stale).
 */
function RowActionsMenu<T>({ row, actions }: RowActionsMenuProps<T>) {
  const [open, setOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const menuRef = React.useRef<HTMLDivElement>(null);
  // Computed fresh each time the menu opens: how much viewport space is actually free above vs.
  // below the trigger, so a row near the bottom (or top) of the screen gets a menu that flips to
  // the side with room and scrolls internally within whatever space it has — never one that gets
  // clipped and needs a scroll to reach the rest of its items.
  const [placement, setPlacement] = React.useState<MenuPlacement | null>(null);

  function toggleOpen() {
    if (!open && triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect();
      const margin = 8;
      const spaceBelow = window.innerHeight - rect.bottom - margin;
      const spaceAbove = rect.top - margin;
      const openUp = spaceBelow < 160 && spaceAbove > spaceBelow;
      const left = Math.min(rect.right - 160, window.innerWidth - 160 - margin);
      setPlacement(
        openUp
          ? { bottom: window.innerHeight - rect.top + 4, left, maxHeight: Math.max(120, spaceAbove) }
          : { top: rect.bottom + 4, left, maxHeight: Math.max(120, spaceBelow) },
      );
    }
    setOpen((o) => !o);
  }

  React.useEffect(() => {
    if (!open) return;
    function onDocMouseDown(e: MouseEvent) {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target)) return;
      if (menuRef.current && !menuRef.current.contains(target)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    function onViewportChange() {
      setOpen(false);
    }
    document.addEventListener('mousedown', onDocMouseDown);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('scroll', onViewportChange, true);
    window.addEventListener('resize', onViewportChange);
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('scroll', onViewportChange, true);
      window.removeEventListener('resize', onViewportChange);
    };
  }, [open]);

  return (
    <>
      <IconButton
        ref={triggerRef}
        variant="ghost"
        size="icon"
        aria-label="Row actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggleOpen}
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
      </IconButton>
      {open &&
        placement &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            style={{
              position: 'fixed',
              top: placement.top,
              bottom: placement.bottom,
              left: placement.left,
              maxHeight: placement.maxHeight,
            }}
            className="z-modal w-40 overflow-y-auto overflow-x-hidden rounded-md border border-default bg-neutral-0 p-1 shadow-md [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {actions.map((action) => (
              <button
                key={action.label}
                type="button"
                role="menuitem"
                onClick={() => {
                  action.onClick(row);
                  setOpen(false);
                }}
                className={cn(
                  'flex w-full items-center rounded-sm px-2 py-1.5 text-left text-body outline-none hover:bg-neutral-100 focus-visible:bg-neutral-100',
                  action.destructive ? 'text-danger-700' : 'text-neutral-800',
                )}
              >
                {action.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}

/* --------------------------------------------------------------- DataTable */

export type SortDirection = 'asc' | 'desc';

export interface Column<T> {
  key: string;
  header: string;
  sortable?: boolean;
  className?: string;
  render?: (row: T) => React.ReactNode;
  accessor?: (row: T) => React.ReactNode;
}

export interface RowAction<T> {
  label: string;
  onClick: (row: T) => void;
  destructive?: boolean;
}

export interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  getRowId: (row: T) => string;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  sort?: { key: string; direction: SortDirection };
  onSortChange?: (key: string, direction: SortDirection) => void;
  rowActions?: (row: T) => RowAction<T>[];
  onRowClick?: (row: T) => void;
  empty?: { title: string; description?: string; action?: { label: string; onClick: () => void } };
  className?: string;
}

export function DataTable<T>({
  columns,
  rows,
  getRowId,
  loading = false,
  error = null,
  onRetry,
  sort,
  onSortChange,
  rowActions,
  onRowClick,
  empty,
  className,
}: DataTableProps<T>) {
  const colSpan = columns.length + (rowActions ? 1 : 0);

  function toggleSort(key: string) {
    const nextDir: SortDirection = sort?.key === key && sort.direction === 'asc' ? 'desc' : 'asc';
    onSortChange?.(key, nextDir);
  }

  return (
    <Table className={className}>
      <thead>
        <tr className="border-b border-subtle text-left text-secondary text-neutral-500">
          {columns.map((col) => (
            <th key={col.key} className={cn('px-3 py-2 font-medium', col.className)} aria-sort={
              sort?.key === col.key ? (sort.direction === 'asc' ? 'ascending' : 'descending') : undefined
            }>
              {col.sortable ? (
                <button
                  type="button"
                  onClick={() => toggleSort(col.key)}
                  className="inline-flex items-center gap-1 hover:text-neutral-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600"
                >
                  {col.header}
                  {sort?.key === col.key ? (
                    sort.direction === 'asc' ? (
                      <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
                    ) : (
                      <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />
                    )
                  ) : (
                    <ChevronsUpDown className="h-3.5 w-3.5 text-neutral-400" aria-hidden="true" />
                  )}
                </button>
              ) : (
                col.header
              )}
            </th>
          ))}
          {rowActions && <th className="w-12 px-3 py-2" aria-label="Actions" />}
        </tr>
      </thead>
      <tbody>
        {loading && (
          <tr data-testid="datatable-loading">
            <td colSpan={colSpan} className="px-3 py-3">
              <div className="flex flex-col gap-2">
                <Skeleton className="h-5 w-full" />
                <Skeleton className="h-5 w-11/12" />
                <Skeleton className="h-5 w-10/12" />
              </div>
            </td>
          </tr>
        )}

        {!loading && error && (
          <tr data-testid="datatable-error">
            <td colSpan={colSpan} className="px-3 py-8 text-center">
              <p className="text-body font-medium text-neutral-900">Unable to load data</p>
              <p className="mt-1 text-secondary text-neutral-500">{error}</p>
              {onRetry && (
                <Button variant="secondary" size="sm" className="mt-3" onClick={onRetry}>
                  Retry
                </Button>
              )}
            </td>
          </tr>
        )}

        {!loading && !error && rows.length === 0 && (
          <tr data-testid="datatable-empty">
            <td colSpan={colSpan}>
              <EmptyState
                title={empty?.title ?? 'No results'}
                description={empty?.description}
                action={empty?.action}
              />
            </td>
          </tr>
        )}

        {!loading && !error &&
          rows.map((row) => (
            <tr
              key={getRowId(row)}
              className={cn('border-b border-subtle', onRowClick && 'cursor-pointer hover:bg-neutral-50')}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((col) => (
                <td key={col.key} className={cn('px-3 py-2 text-neutral-800', col.className)}>
                  {col.render ? col.render(row) : col.accessor ? col.accessor(row) : null}
                </td>
              ))}
              {rowActions && (
                <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                  <RowActionsMenu row={row} actions={rowActions(row)} />
                </td>
              )}
            </tr>
          ))}
      </tbody>
    </Table>
  );
}
