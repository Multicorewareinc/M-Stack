import * as AvatarPrimitive from '@radix-ui/react-avatar';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { ChevronRight } from 'lucide-react';
import * as React from 'react';
import { cn } from '../styles/cn';

/* ------------------------------------------------------------- PageHeader */

export interface PageHeaderProps {
  title: string;
  description?: string;
  /** Small uppercase label above the title, e.g. "PLATFORM CONTROL PLANE / ORGANIZATIONS". Decorative only. */
  eyebrow?: string;
  actions?: React.ReactNode;
  className?: string;
}

export function PageHeader({ title, description, eyebrow, actions, className }: PageHeaderProps) {
  return (
    <header className={cn('flex items-start justify-between gap-4 pb-4', className)}>
      <div className="flex flex-col gap-1">
        {eyebrow && (
          <p className="text-caption font-semibold uppercase tracking-wide text-neutral-500">{eyebrow}</p>
        )}
        <h1 className="text-pageTitle font-semibold text-neutral-900">{title}</h1>
        {description && <p className="text-secondary text-neutral-500">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

/* ------------------------------------------------------------ Breadcrumbs */

export interface Crumb {
  label: string;
  href?: string;
}

export function Breadcrumbs({ items, className }: { items: Crumb[]; className?: string }) {
  return (
    <nav aria-label="Breadcrumb" className={cn('flex items-center gap-1 text-secondary text-neutral-500', className)}>
      {items.map((item, i) => (
        <React.Fragment key={`${item.label}-${i}`}>
          {i > 0 && <ChevronRight className="h-3.5 w-3.5 text-neutral-400" aria-hidden="true" />}
          {item.href && i < items.length - 1 ? (
            <a href={item.href} className="hover:text-neutral-700">
              {item.label}
            </a>
          ) : (
            <span aria-current={i === items.length - 1 ? 'page' : undefined} className="text-neutral-700">
              {item.label}
            </span>
          )}
        </React.Fragment>
      ))}
    </nav>
  );
}

/* ------------------------------------------------------------------- Tabs */

export const Tabs = TabsPrimitive.Root;

export const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn('inline-flex items-center gap-1 border-b border-subtle', className)}
    {...props}
  />
));
TabsList.displayName = 'TabsList';

export const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      '-mb-px border-b-2 border-transparent px-3 py-2 text-body font-medium text-neutral-500 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 data-[state=active]:border-primary-600 data-[state=active]:text-neutral-900',
      className,
    )}
    {...props}
  />
));
TabsTrigger.displayName = 'TabsTrigger';

export const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content ref={ref} className={cn('pt-4 focus-visible:outline-none', className)} {...props} />
));
TabsContent.displayName = 'TabsContent';

/* --------------------------------------------------------------- StatCard */

export interface StatCardProps {
  label: string;
  value: React.ReactNode;
  hint?: string;
  className?: string;
  /** When provided, the card renders as an interactive button instead of a static div. */
  onClick?: () => void;
  /** Pressed/active visual state — for a card that toggles a filter, e.g. a status count. */
  selected?: boolean;
}

export function StatCard({ label, value, hint, className, onClick, selected }: StatCardProps) {
  const content = (
    <>
      <p className="text-secondary text-neutral-500">{label}</p>
      <p className="text-pageTitle font-semibold text-neutral-900">{value}</p>
      {hint && <p className="text-caption text-neutral-400">{hint}</p>}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        aria-pressed={selected}
        className={cn(
          'flex flex-col gap-1 rounded-lg border p-4 text-left transition-colors hover:bg-neutral-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600',
          selected ? 'border-primary-600 bg-primary-50' : 'border-subtle bg-neutral-0',
          className,
        )}
      >
        {content}
      </button>
    );
  }

  return (
    <div className={cn('flex flex-col gap-1 rounded-lg border border-subtle bg-neutral-0 p-4', className)}>
      {content}
    </div>
  );
}

/* ----------------------------------------------------------------- Avatar */

export interface AvatarProps {
  name: string;
  src?: string;
  className?: string;
}

export function Avatar({ name, src, className }: AvatarProps) {
  const initials = name
    .split(' ')
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase();
  return (
    <AvatarPrimitive.Root
      className={cn('inline-flex h-8 w-8 select-none items-center justify-center overflow-hidden rounded-full bg-neutral-200', className)}
    >
      <AvatarPrimitive.Image src={src} alt={name} className="h-full w-full object-cover" />
      <AvatarPrimitive.Fallback className="text-caption font-medium text-neutral-600" delayMs={0}>
        {initials}
      </AvatarPrimitive.Fallback>
    </AvatarPrimitive.Root>
  );
}
