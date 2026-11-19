import { cn } from '@multistack/ui';
import { NavLink } from 'react-router-dom';
import { useAuth } from '../../auth/AuthContext';

export interface NavItem {
  label: string;
  to: string;
  end?: boolean;
  /** Omit for nav items open to every authenticated user (Overview, Chat, Billing, Settings).
   * An array matches with OR semantics (any one grants visibility). */
  permission?: string | string[];
}

export const ORG_NAV: NavItem[] = [
  { label: 'Overview', to: '/', end: true },
  { label: 'Users', to: '/users', permission: 'users.read' },
  // Inviting, listing and revoking invitations all need users.create (matches the server).
  { label: 'Invitations', to: '/invitations', permission: 'users.create' },
  { label: 'Roles', to: '/roles', permission: 'roles.read' },
  // No dedicated "permissions.read" slug exists in the master catalog — reuses roles.read (see
  // routes.tsx's matching RequirePermission for the same reasoning).
  { label: 'Permissions', to: '/permissions', permission: 'roles.read' },
  { label: 'API Keys', to: '/api-keys', permission: ['apikey.create', 'apikey.update', 'apikey.delete'] },
  { label: 'Audit Logs', to: '/audit-logs' },
  { label: 'Chat', to: '/chat' },
  { label: 'Billing & Usage', to: '/billing' },
];

/** A nav item with no `permission` is open to everyone. Reads `useAuth`'s raw `permissions` list
 * directly (exactly what usePermission does under the hood) rather than calling usePermission
 * per item in a .filter callback — that would be a conditional/looped hook call. */
function useVisibleNavItems(items: NavItem[]): NavItem[] {
  const { permissions } = useAuth();
  return items.filter((item) => {
    if (!item.permission) return true;
    const required = Array.isArray(item.permission) ? item.permission : [item.permission];
    return required.some((slug) => permissions.includes(slug));
  });
}

export function Sidebar() {
  const { organizationContext } = useAuth();
  const visibleNav = useVisibleNavItems(ORG_NAV);
  return (
    <nav aria-label="Primary" className="flex w-56 shrink-0 flex-col gap-1 border-r border-subtle bg-neutral-0 p-3">
      <div className="mb-2 border-b border-subtle px-2 pb-2">
        <p className="text-body font-semibold text-neutral-900">{organizationContext.name}</p>
      </div>
      {visibleNav.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) =>
            cn(
              'rounded-md px-2 py-1.5 text-body font-medium text-neutral-600 hover:bg-neutral-100',
              isActive && 'border-l-2 border-primary-600 bg-primary-50 font-semibold text-primary-700',
            )
          }
        >
          {item.label}
        </NavLink>
      ))}
      <div className="mt-auto border-t border-subtle pt-2">
        <NavLink
          to="/settings"
          className={({ isActive }) =>
            cn(
              'rounded-md px-2 py-1.5 text-body font-medium text-neutral-600 hover:bg-neutral-100',
              isActive && 'border-l-2 border-primary-600 bg-primary-50 font-semibold text-primary-700',
            )
          }
        >
          Settings
        </NavLink>
      </div>
    </nav>
  );
}
