import * as React from 'react';
import { cn } from '../styles/cn';
import { Checkbox } from './inputs';

/* ----------------------------------------------------------- PermissionMatrix */

export interface MatrixPermission {
  id: string;
  resource: string;
  action: string;
  slug?: string;
  description?: string;
}

export interface PermissionMatrixProps {
  permissions: MatrixPermission[];
  selectedIds: string[];
  onChange: (selectedIds: string[]) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * Generic resource→action permission grid. Business-agnostic: it renders the
 * supplied permission list grouped by resource and emits the updated
 * selected-id set on toggle. It carries no catalog knowledge and never mutates
 * or derives the payload — the emitted array is exactly the resulting selection.
 */
export function PermissionMatrix({
  permissions,
  selectedIds,
  onChange,
  disabled = false,
  className,
}: PermissionMatrixProps) {
  const selected = React.useMemo(() => new Set(selectedIds), [selectedIds]);

  const groups = React.useMemo(() => {
    const map = new Map<string, MatrixPermission[]>();
    for (const p of permissions) {
      const list = map.get(p.resource) ?? [];
      list.push(p);
      map.set(p.resource, list);
    }
    return [...map.entries()];
  }, [permissions]);

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange([...next]);
  }

  return (
    <div className={cn('flex flex-col gap-5', className)}>
      {groups.map(([resource, perms]) => (
        <section key={resource} className="flex flex-col gap-2">
          <h4 className="border-b border-subtle pb-1 text-secondary font-semibold capitalize text-neutral-700">
            {resource}
          </h4>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
            {perms.map((p) => {
              const inputId = `perm-${p.id}`;
              return (
                <label key={p.id} htmlFor={inputId} className="flex items-center gap-2 text-body text-neutral-800">
                  <Checkbox
                    id={inputId}
                    checked={selected.has(p.id)}
                    disabled={disabled}
                    onCheckedChange={() => toggle(p.id)}
                  />
                  <span className="capitalize">{p.action}</span>
                </label>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------- RoleSelector */

export interface SelectableRole {
  id: string;
  name: string;
  description?: string;
}

export interface RoleSelectorProps {
  roles: SelectableRole[];
  selectedIds: string[];
  onChange: (selectedIds: string[]) => void;
  disabled?: boolean;
  className?: string;
}

/** Generic multi-select of roles; emits the updated selected role-id set. */
export function RoleSelector({ roles, selectedIds, onChange, disabled = false, className }: RoleSelectorProps) {
  const selected = React.useMemo(() => new Set(selectedIds), [selectedIds]);

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange([...next]);
  }

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      {roles.map((role) => {
        const inputId = `role-${role.id}`;
        return (
          // The description is a sibling of <label>, not nested inside it —
          // nesting it would concatenate into the label's plain-textContent
          // accessible name (e.g. "AdminOrganization administrator"),
          // breaking exact-match getByLabelText(role.name) queries (same
          // class of bug as FormField's required-asterisk fix).
          <div key={role.id} className="flex items-start gap-2 text-body text-neutral-800">
            <Checkbox
              id={inputId}
              checked={selected.has(role.id)}
              disabled={disabled}
              onCheckedChange={() => toggle(role.id)}
              className="mt-0.5"
            />
            <div className="flex flex-col">
              <label htmlFor={inputId} className="font-medium">
                {role.name}
              </label>
              {role.description && <span className="text-secondary text-neutral-500">{role.description}</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
