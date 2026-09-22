import { cva, type VariantProps } from 'class-variance-authority';
import { AlertTriangle, CheckCircle2, CircleDashed, HelpCircle, Loader2, PauseCircle } from 'lucide-react';
import * as React from 'react';
import { cn } from '../styles/cn';

/* ------------------------------------------------------------------ Badge */

const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-caption font-medium',
  {
    variants: {
      variant: {
        neutral: 'bg-neutral-100 text-neutral-700',
        primary: 'bg-primary-100 text-primary-700',
        success: 'bg-success-100 text-success-700',
        warning: 'bg-warning-100 text-warning-700',
        danger: 'bg-danger-100 text-danger-700',
        info: 'bg-info-100 text-info-700',
      },
    },
    defaultVariants: { variant: 'neutral' },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

/* ------------------------------------------------------------ StatusBadge */

// Real backend status values are lowercase (OrgStatus / User status Literals); there is no
// "deleting" status anywhere server-side — deletion is a synchronous hard delete, not a
// transitional state (confirmed against api/microservices/*/src).
export const STATUSES = ['active', 'provisioning', 'failed', 'suspended'] as const;
export type Status = (typeof STATUSES)[number];

const STATUS_CONFIG: Record<
  Status,
  { label: string; variant: NonNullable<BadgeProps['variant']>; Icon: typeof CheckCircle2 }
> = {
  active: { label: 'Active', variant: 'success', Icon: CheckCircle2 },
  provisioning: { label: 'Provisioning', variant: 'info', Icon: Loader2 },
  failed: { label: 'Failed', variant: 'danger', Icon: AlertTriangle },
  suspended: { label: 'Suspended', variant: 'warning', Icon: PauseCircle },
};

const UNKNOWN = { label: 'Unknown', variant: 'neutral' as const, Icon: HelpCircle };

export interface StatusBadgeProps {
  status: string;
  className?: string;
}

/**
 * Renders a backend status. Only the fixed vocabulary is recognized; anything
 * else falls back to "Unknown" (never the raw value). Status is conveyed by
 * label + icon shape, never color alone, with an accessible label.
 */
export function StatusBadge({ status, className }: StatusBadgeProps) {
  const cfg = (STATUS_CONFIG as Record<string, (typeof STATUS_CONFIG)[Status] | undefined>)[status] ?? UNKNOWN;
  const { label, variant, Icon } = cfg;
  const isKnown = cfg !== UNKNOWN;
  return (
    <Badge variant={variant} className={className} role="status" aria-label={`Status: ${label}`}>
      <Icon
        className={cn('h-3 w-3', status === 'provisioning' && 'animate-spin')}
        aria-hidden="true"
        data-icon={isKnown ? status : 'unknown'}
      />
      {label}
    </Badge>
  );
}

/* ----------------------------------------------------------- Spinner (re-exported here for status colocation) */

export function Spinner({ className, label = 'Loading' }: { className?: string; label?: string }) {
  return (
    <span role="status" aria-label={label} className="inline-flex">
      <Loader2 className={cn('h-4 w-4 animate-spin text-neutral-500', className)} aria-hidden="true" />
    </span>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cn('animate-pulse rounded-md bg-neutral-200', className)} />;
}

export { CircleDashed as _CircleDashed };
