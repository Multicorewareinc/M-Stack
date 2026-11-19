import { Badge, DetailDialog } from '@multistack/ui';
import type { AuditLog } from '../../api/types';
import { formatDate } from '../../lib/formatters';
import { actionLabel, actionVariant, actorLabel, actorTypeLabel } from './auditActions';

export interface AuditLogDetailDialogProps {
  log: AuditLog | null;
  /** Resolved organization name, when the event belongs to one. */
  organizationName?: string;
  onClose: () => void;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-secondary text-neutral-500">{label}</dt>
      <dd className="break-words text-body text-neutral-900">{children}</dd>
    </div>
  );
}

const orDash = (value: string | null | undefined) => value || '—';

/** Everything the table row leaves out: full actor identity, target ids, IP address, and the raw
 * `details` payload the control plane recorded with the event. */
export function AuditLogDetailDialog({ log, organizationName, onClose }: AuditLogDetailDialogProps) {
  const details = log ? Object.entries(log.details ?? {}) : [];
  return (
    <DetailDialog open={log !== null} onOpenChange={(open) => !open && onClose()} label="Audit event details">
      {log && (
        <div className="flex flex-col gap-5">
          <div className="flex flex-col gap-1 pr-8">
            <h2 className="text-cardTitle font-semibold text-neutral-900">Audit event</h2>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={actionVariant(log.action)}>{actionLabel(log.action)}</Badge>
              <span className="text-secondary text-neutral-500">{formatDate(log.created_at)}</span>
            </div>
          </div>

          <dl className="grid gap-4 sm:grid-cols-2">
            <Field label="Actor">{actorLabel(log)}</Field>
            <Field label="Actor type">{actorTypeLabel(log.actor_type)}</Field>
            <Field label="Actor ID">{orDash(log.actor_id)}</Field>
            <Field label="IP address">{orDash(log.ip_address)}</Field>
            <Field label="Target">{orDash(log.resource_name ?? log.resource_id)}</Field>
            <Field label="Target type">{orDash(log.resource_type)}</Field>
            <Field label="Target ID">{orDash(log.resource_id)}</Field>
            <Field label="Organization">
              {log.organization_id ? (organizationName ?? log.organization_id) : 'Platform'}
            </Field>
            <Field label="Source">{log.source === 'platform' ? 'Platform control plane' : 'Organization control plane'}</Field>
            <Field label="Event ID">{log.id}</Field>
          </dl>

          <section aria-label="Event payload" className="flex flex-col gap-2">
            <h3 className="text-body font-medium text-neutral-900">Additional details</h3>
            {details.length === 0 ? (
              <p className="text-secondary text-neutral-500">No additional details were recorded for this event.</p>
            ) : (
              <pre className="max-h-64 overflow-auto rounded-md border border-subtle bg-neutral-50 p-3 text-secondary text-neutral-800">
                {JSON.stringify(log.details, null, 2)}
              </pre>
            )}
          </section>
        </div>
      )}
    </DetailDialog>
  );
}
