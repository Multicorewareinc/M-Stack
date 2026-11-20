import type { BadgeProps } from '@multistack/ui';
import type { DeploymentStatus, HealthStatus } from '../api/client';

type Variant = NonNullable<BadgeProps['variant']>;

export const DEPLOYMENT_STATUSES: DeploymentStatus[] = [
  'pending',
  'validating',
  'provisioning',
  'provisioned',
  'degraded',
  'failed',
  'deleting',
];

export const HEALTH_STATUSES: HealthStatus[] = ['healthy', 'degraded', 'unhealthy', 'unknown'];

const DEPLOYMENT_META: Record<DeploymentStatus, { label: string; variant: Variant }> = {
  pending: { label: 'Pending', variant: 'neutral' },
  validating: { label: 'Validating', variant: 'info' },
  provisioning: { label: 'Provisioning', variant: 'info' },
  provisioned: { label: 'Provisioned', variant: 'success' },
  degraded: { label: 'Degraded', variant: 'warning' },
  failed: { label: 'Failed', variant: 'danger' },
  deleting: { label: 'Deleting', variant: 'warning' },
};

const HEALTH_META: Record<HealthStatus, { label: string; variant: Variant }> = {
  unknown: { label: 'Unknown', variant: 'neutral' },
  healthy: { label: 'Healthy', variant: 'success' },
  degraded: { label: 'Degraded', variant: 'warning' },
  unhealthy: { label: 'Unhealthy', variant: 'danger' },
};

// A status the SDK adds later still renders (neutral, title-cased) rather than breaking the page.
const fallback = (value: string) => ({ label: value.charAt(0).toUpperCase() + value.slice(1), variant: 'neutral' as Variant });

export const deploymentMeta = (status: string) => DEPLOYMENT_META[status as DeploymentStatus] ?? fallback(status);
export const healthMeta = (status: string) => HEALTH_META[status as HealthStatus] ?? fallback(status);

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** An ISO-8601 timestamp as "16 Sep 2026, 07:25:09" in the viewer's local time. The state db stores
 * UTC; seconds are kept because deployment steps often finish within the same minute. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getDate())} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** "just now", "5 min ago", "3 h ago", "2 d ago". */
export function timeAgo(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return '—';
  const t = new Date(value).getTime();
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 10) return 'just now';
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
