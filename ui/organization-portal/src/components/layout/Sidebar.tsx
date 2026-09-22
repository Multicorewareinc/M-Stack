import { cn } from '@multistack/ui';
import { NavLink } from 'react-router-dom';
import { useAuth } from '../../auth/AuthContext';

export interface NavItem {
  label: string;
  to: string;
  end?: boolean;
}

export const ORG_NAV: NavItem[] = [
  { label: 'Overview', to: '/', end: true },
  { label: 'Users', to: '/users' },
  { label: 'Roles', to: '/roles' },
  { label: 'Permissions', to: '/permissions' },
  { label: 'API Keys', to: '/api-keys' },
  { label: 'Chat', to: '/chat' },
  { label: 'Billing & Usage', to: '/billing' },
];

export function Sidebar() {
  const { organizationContext } = useAuth();
  return (
    <nav aria-label="Primary" className="flex w-56 shrink-0 flex-col gap-1 border-r border-subtle bg-neutral-0 p-3">
      <p className="px-2 pb-1 text-caption font-semibold uppercase tracking-wide text-neutral-500">Multistack</p>
      <p className="px-2 pb-2 text-body font-medium text-neutral-700">{organizationContext.name}</p>
      {ORG_NAV.map((item) => (
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
