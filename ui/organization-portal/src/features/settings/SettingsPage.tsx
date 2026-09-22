import { Button, PageHeader, StatusBadge } from '@multistack/ui';
import * as React from 'react';
import { formatQuota } from '../../lib/formatters';
import { useOrgId } from '../users/hooks/useOrgId';
import { useOrgSummary } from './hooks/useOrgSummary';

function CopyOrgId({ orgId }: { orgId: string }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <Button
      variant="secondary"
      size="sm"
      onClick={() => {
        navigator.clipboard.writeText(orgId);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? 'Copied' : 'Copy'}
    </Button>
  );
}

/** Read-only (§72, §229) — the Organization Control Plane exposes no self-service update endpoint for organizations today. */
export function SettingsPage() {
  const orgId = useOrgId();
  const { data: summary, isLoading, error } = useOrgSummary();

  return (
    <div>
      <PageHeader title="Settings" />
      {isLoading && <p className="text-body text-neutral-500">Loading settings…</p>}
      {error && <p className="text-body text-danger-500">{error.message}</p>}
      {summary && (
        <div className="flex flex-col gap-6">
          <section>
            <h2 className="text-sectionTitle font-semibold text-neutral-900">Profile</h2>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-body">
              <dt className="text-neutral-500">Name</dt>
              <dd className="text-neutral-900">{summary.organization.name}</dd>
              <dt className="text-neutral-500">Slug</dt>
              <dd className="text-neutral-900">{summary.organization.slug}</dd>
              <dt className="text-neutral-500">Organization ID</dt>
              <dd className="flex items-center gap-2 text-neutral-900">
                <span>{orgId}</span>
                <CopyOrgId orgId={orgId} />
              </dd>
              <dt className="text-neutral-500">Status</dt>
              <dd>
                <StatusBadge status={summary.organization.status} />
              </dd>
            </dl>
          </section>
          <section>
            <h2 className="text-sectionTitle font-semibold text-neutral-900">Plan</h2>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-body">
              <dt className="text-neutral-500">Plan</dt>
              <dd className="text-neutral-900">{summary.plan.name}</dd>
              <dt className="text-neutral-500">TPM limit</dt>
              <dd className="text-neutral-900">{formatQuota(summary.plan.tpm)}</dd>
              <dt className="text-neutral-500">RPM limit</dt>
              <dd className="text-neutral-900">{formatQuota(summary.plan.rpm)}</dd>
              <dt className="text-neutral-500">Quota limit</dt>
              <dd className="text-neutral-900">{formatQuota(summary.plan.quota_monthly_tokens)}</dd>
            </dl>
          </section>
        </div>
      )}
    </div>
  );
}
