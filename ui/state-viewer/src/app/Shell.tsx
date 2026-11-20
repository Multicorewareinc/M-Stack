import { Button, Select, cn } from '@multistack/ui';
import { useIsFetching, useQueryClient } from '@tanstack/react-query';
import { NavLink, Outlet } from 'react-router-dom';
import { DeploymentDetailHost } from '../features/deployments/DeploymentDetail';
import { useInfo } from '../features/state/hooks';
import { formatDateTime } from '../lib/format';
import { REFRESH_OPTIONS, useRefresh } from './RefreshContext';

const NAV = [
  { label: 'Deployments', to: '/', end: true },
  { label: 'Activity', to: '/activity', end: false },
];

export function Shell() {
  const queryClient = useQueryClient();
  const { value, setValue } = useRefresh();
  const { data: info } = useInfo();
  const fetching = useIsFetching() > 0;

  return (
    <div className="flex h-screen flex-col">
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-subtle bg-neutral-0 px-4">
        <span className="whitespace-nowrap text-body font-semibold text-neutral-900">Multistack State Viewer</span>
        <div className="flex shrink-0 items-center gap-3">
          {info && (
            <span className="text-caption text-neutral-500" aria-live="polite">
              {fetching ? 'Refreshing…' : `Updated ${formatDateTime(info.generated_at)}`}
            </span>
          )}
          <Select
            aria-label="Auto-refresh"
            value={value}
            onValueChange={setValue}
            options={REFRESH_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
            className="w-40"
          />
          <Button variant="secondary" onClick={() => queryClient.invalidateQueries()}>
            Refresh
          </Button>
        </div>
      </header>
      <div className="flex flex-1 overflow-hidden">
        <nav aria-label="Primary" className="flex w-52 shrink-0 flex-col gap-1 border-r border-subtle bg-neutral-0 p-3">
          {NAV.map((item) => (
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
        </nav>
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
      <DeploymentDetailHost />
    </div>
  );
}
