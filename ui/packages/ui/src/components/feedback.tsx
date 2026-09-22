import { cva, type VariantProps } from 'class-variance-authority';
import { AlertCircle, CheckCircle2, Info, XCircle } from 'lucide-react';
import * as React from 'react';
import { cn } from '../styles/cn';
import { Button } from './inputs';

/* ------------------------------------------------------------------ Alert */

const alertVariants = cva('flex gap-3 rounded-md border p-3 text-body', {
  variants: {
    variant: {
      info: 'border-info-500/30 bg-info-100 text-info-700',
      success: 'border-success-500/30 bg-success-100 text-success-700',
      warning: 'border-warning-500/30 bg-warning-100 text-warning-700',
      error: 'border-danger-500/30 bg-danger-100 text-danger-700',
    },
  },
  defaultVariants: { variant: 'info' },
});

const VARIANT_ICON = {
  info: Info,
  success: CheckCircle2,
  warning: AlertCircle,
  error: XCircle,
} as const;

export interface AlertProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof alertVariants> {
  title?: string;
}

export function Alert({ className, variant = 'info', title, children, ...props }: AlertProps) {
  const Icon = VARIANT_ICON[variant ?? 'info'];
  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      className={cn(alertVariants({ variant }), className)}
      {...props}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="flex flex-col gap-0.5">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className="text-secondary">{children}</div>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ Toast */

export type ToastVariant = 'success' | 'warning' | 'error' | 'info';

export interface ToastProps {
  variant: ToastVariant;
  title: string;
  description?: string;
  className?: string;
}

/**
 * Presentational toast. Error toasts get an assertive alert role; the others
 * are polite status. A real toaster/provider lives in the app layer (SP-02);
 * this is the visual unit only.
 */
export function Toast({ variant, title, description, className }: ToastProps) {
  const Icon = VARIANT_ICON[variant];
  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      aria-live={variant === 'error' ? 'assertive' : 'polite'}
      className={cn(
        'z-toast flex w-80 gap-3 rounded-md border border-default bg-neutral-0 p-3 shadow-lg',
        className,
      )}
      data-variant={variant}
    >
      <Icon
        className={cn(
          'mt-0.5 h-4 w-4 shrink-0',
          variant === 'success' && 'text-success-500',
          variant === 'warning' && 'text-warning-500',
          variant === 'error' && 'text-danger-500',
          variant === 'info' && 'text-info-500',
        )}
        aria-hidden="true"
      />
      <div className="flex flex-col gap-0.5">
        <p className="text-body font-medium text-neutral-900">{title}</p>
        {description && <p className="text-secondary text-neutral-500">{description}</p>}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------- EmptyState */

export interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3 px-6 py-12 text-center', className)}>
      {icon && <div className="text-neutral-400">{icon}</div>}
      <div className="flex flex-col gap-1">
        <p className="text-cardTitle font-semibold text-neutral-900">{title}</p>
        {description && <p className="text-secondary text-neutral-500">{description}</p>}
      </div>
      {action && (
        <Button onClick={action.onClick} size="sm">
          {action.label}
        </Button>
      )}
    </div>
  );
}
