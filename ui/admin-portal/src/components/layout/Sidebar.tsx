import { cn } from '@multistack/ui';
import { NavLink } from 'react-router-dom';
import { BRANDING } from '../../branding';

export interface NavItem {
  label: string;
  to: string;
  end?: boolean;
}

export const ADMIN_NAV: NavItem[] = [
  { label: 'Overview', to: '/', end: true },
  { label: 'Organizations', to: '/organizations' },
  { label: 'Plans', to: '/plans' },
  { label: 'Permissions', to: '/permissions' },
  { label: 'Users', to: '/users' },
  { label: 'Invitations', to: '/invitations' },
  { label: 'Audit Logs', to: '/audit-logs' },
];

export function Sidebar() {
  return (
    <nav aria-label="Primary" className="flex w-56 shrink-0 flex-col gap-1 border-r border-subtle bg-neutral-0 p-3">
      <div className="mb-2 border-b border-subtle px-2 pb-2">
        <p className="text-caption font-semibold uppercase tracking-wide text-neutral-500">{BRANDING.appName}</p>
      </div>
      {ADMIN_NAV.map((item) => (
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
