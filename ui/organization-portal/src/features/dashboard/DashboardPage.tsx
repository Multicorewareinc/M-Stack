import { PageHeader, StatCard } from '@multistack/ui';
import { formatQuota } from '../../lib/formatters';
import { useOrgSummary } from '../settings/hooks/useOrgSummary';

/** Overview landing page (§53-55). No charts (§125), no admin-only controls (§54). */
export function DashboardPage() {
  const { data: summary, isLoading, error } = useOrgSummary();

  return (
    <div>
      <PageHeader title="Overview" />
      {error && <p className="text-body text-danger-500">{error.message}</p>}
      {!error && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
          <StatCard label="Users" value={isLoading ? '—' : summary?.user_count ?? 0} />
          <StatCard label="Roles" value={isLoading ? '—' : summary?.role_count ?? 0} />
          <StatCard label="Plan" value={isLoading ? '—' : summary?.plan.name ?? '—'} />
          <StatCard label="TPM limit" value={isLoading ? '—' : formatQuota(summary?.plan.tpm ?? 0)} />
          <StatCard label="RPM limit" value={isLoading ? '—' : formatQuota(summary?.plan.rpm ?? 0)} />
          <StatCard label="Quota limit" value={isLoading ? '—' : formatQuota(summary?.plan.quota_monthly_tokens ?? 0)} />
        </div>
      )}
    </div>
  );
}
