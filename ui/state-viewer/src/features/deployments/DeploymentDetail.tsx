import { Alert, Badge, DataTable, DetailDialog, Skeleton, type Column } from '@multistack/ui';
import { useSearchParams } from 'react-router-dom';
import type { HealthCheck } from '../../api/client';
import { deploymentMeta, formatDateTime, healthMeta, timeAgo } from '../../lib/format';
import { useNow } from '../../lib/useNow';
import { useDeployment } from '../state/hooks';

const PARAM = 'd';

/** The selected deployment lives in the URL (?d=<name>) so a detail view can be linked to and survives
 * a reload. A query parameter rather than a path segment: route names look like "namespace/name". */
export function useSelectedDeployment() {
  const [params, setParams] = useSearchParams();
  const selected = params.get(PARAM);
  return {
    selected,
    select: (name: string | null) => {
      const next = new URLSearchParams(params);
      if (name) next.set(PARAM, name);
      else next.delete(PARAM);
      setParams(next);
    },
  };
}

const columns: Column<HealthCheck>[] = [
  { key: 'time', header: 'Time', accessor: (h) => formatDateTime(h.checked_at) },
  { key: 'status', header: 'Result', render: (h) => <Badge variant={healthMeta(h.status).variant}>{healthMeta(h.status).label}</Badge> },
  { key: 'detail', header: 'Detail', accessor: (h) => h.detail },
];

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-caption text-neutral-500">{label}</dt>
      <dd className="text-body text-neutral-900">{children}</dd>
    </div>
  );
}

function DeploymentDetail({ name }: { name: string }) {
  const { data, error, isLoading } = useDeployment(name);
  const now = useNow();

  if (isLoading) return <Skeleton className="h-40 w-full" />;
  if (error || !data) {
    return (
      <Alert variant="warning" title={`Cannot show “${name}”`}>
        {error?.message ?? 'Not found.'} It may have been deleted since the list was loaded.
      </Alert>
    );
  }

  const status = deploymentMeta(data.status);
  return (
    <div className="flex flex-col gap-5">
      <div>
        <p className="text-caption uppercase tracking-wide text-neutral-500">{data.component_type}</p>
        <h2 className="text-pageTitle font-semibold text-neutral-900">{data.name}</h2>
      </div>

      <dl className="grid grid-cols-2 gap-4">
        <Field label="Status">
          <Badge variant={status.variant}>{status.label}</Badge>
        </Field>
        <Field label="Last updated">
          {formatDateTime(data.updated_at)} <span className="text-neutral-500">({timeAgo(data.updated_at, now)})</span>
        </Field>
        <Field label="First recorded">{formatDateTime(data.created_at)}</Field>
        <Field label="Kubeconfig">
          {data.kubeconfig_path ? <span className="font-mono">{data.kubeconfig_path}</span> : '—'}
        </Field>
      </dl>

      {data.error && (
        <Alert variant="error" title="Error">
          <span className="whitespace-pre-wrap break-words font-mono text-caption">{data.error}</span>
        </Alert>
      )}

      <section aria-label="Health history">
        <h3 className="mb-2 text-body font-semibold text-neutral-900">Status history</h3>
        <DataTable
          columns={columns}
          rows={data.health_checks}
          getRowId={(h) => String(h.id)}
          empty={{ title: 'No history yet', description: 'A result is recorded when this deployment finishes, degrades or fails.' }}
        />
        <p className="mt-2 text-caption text-neutral-500">
          Only outcomes are recorded today (healthy, degraded, failed) — not the individual steps in between.
        </p>
      </section>
    </div>
  );
}

export function DeploymentDetailHost() {
  const { selected, select } = useSelectedDeployment();
  return (
    <DetailDialog
      open={selected !== null}
      onOpenChange={(open) => !open && select(null)}
      label={selected ? `Deployment ${selected}` : 'Deployment'}
    >
      {selected && <DeploymentDetail name={selected} />}
    </DetailDialog>
  );
}
