import * as DialogPrimitive from '@radix-ui/react-dialog';
import * as DropdownMenuPrimitive from '@radix-ui/react-dropdown-menu';
import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import { X } from 'lucide-react';
import * as React from 'react';
import { cn } from '../styles/cn';
import { Button } from './inputs';
import { Alert } from './feedback';
import { Spinner } from './status';

/* ---------------------------------------------------------------- Dialog */

export interface DialogProps {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  trigger?: React.ReactNode;
  title: string;
  description?: string;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}

const overlayClass = 'fixed inset-0 z-overlay bg-black/60';

export function Dialog({ open, onOpenChange, trigger, title, description, children, footer, className }: DialogProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      {trigger && <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>}
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={overlayClass} />
        <DialogPrimitive.Content
          className={cn(
            'fixed left-1/2 top-1/2 z-modal flex w-full max-w-md -translate-x-1/2 -translate-y-1/2 flex-col gap-4 rounded-lg border border-default bg-neutral-0 p-5 shadow-lg focus:outline-none',
            className,
          )}
        >
          <div className="flex flex-col gap-1">
            <DialogPrimitive.Title className="text-cardTitle font-semibold text-neutral-900">
              {title}
            </DialogPrimitive.Title>
            {description && (
              <DialogPrimitive.Description className="text-secondary text-neutral-500">
                {description}
              </DialogPrimitive.Description>
            )}
          </div>
          {children}
          {footer && <div className="flex justify-end gap-2">{footer}</div>}
          <DialogPrimitive.Close asChild>
            <button aria-label="Close" className="absolute right-3 top-3 rounded-sm p-1 text-neutral-400 hover:text-neutral-700">
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </DialogPrimitive.Close>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/* ---------------------------------------------------------- DetailDialog */

export interface DetailDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Accessible name for screen readers only — the content supplies its own visible heading. */
  label: string;
  children?: React.ReactNode;
  className?: string;
}

/**
 * A wide modal shell for showing an existing page's content (e.g. a detail
 * page) as a popup instead of a full navigation. Unlike Dialog, it has no
 * built-in title bar/footer — the children render their own heading/actions,
 * so nothing is duplicated. Only a visually-hidden title is set for a11y.
 */
export function DetailDialog({ open, onOpenChange, label, children, className }: DetailDialogProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={overlayClass} />
        <DialogPrimitive.Content
          className={cn(
            'fixed left-1/2 top-1/2 z-modal flex max-h-[85vh] w-full max-w-3xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto rounded-lg border border-default bg-neutral-0 p-6 shadow-lg focus:outline-none',
            className,
          )}
        >
          <DialogPrimitive.Title className="sr-only">{label}</DialogPrimitive.Title>
          <DialogPrimitive.Close asChild>
            <button
              aria-label="Close"
              className="absolute right-3 top-3 rounded-sm p-1 text-neutral-400 hover:text-neutral-700"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </DialogPrimitive.Close>
          {children}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/* ----------------------------------------------------------------- Drawer */

export interface DrawerProps extends DialogProps {
  side?: 'right' | 'left';
}

export function Drawer({ open, onOpenChange, trigger, title, description, children, footer, side = 'right', className }: DrawerProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      {trigger && <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>}
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={overlayClass} />
        <DialogPrimitive.Content
          className={cn(
            'fixed inset-y-0 z-modal flex w-full max-w-md flex-col gap-4 border-default bg-neutral-0 p-5 shadow-lg focus:outline-none',
            side === 'right' ? 'right-0 border-l' : 'left-0 border-r',
            className,
          )}
        >
          <div className="flex flex-col gap-1">
            <DialogPrimitive.Title className="text-cardTitle font-semibold text-neutral-900">
              {title}
            </DialogPrimitive.Title>
            {description && (
              <DialogPrimitive.Description className="text-secondary text-neutral-500">
                {description}
              </DialogPrimitive.Description>
            )}
          </div>
          <div className="flex-1 overflow-y-auto">{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t border-subtle pt-4">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/* ----------------------------------------------------------- ConfirmDialog */

export interface ConfirmDialogProps {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  trigger?: React.ReactNode;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  loading?: boolean;
  destructive?: boolean;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  trigger,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  onConfirm,
  loading = false,
  destructive = false,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      trigger={trigger}
      title={title}
      description={description}
      footer={
        <>
          <DialogPrimitive.Close asChild>
            <Button variant="secondary" disabled={loading}>
              {cancelLabel}
            </Button>
          </DialogPrimitive.Close>
          <Button
            variant={destructive ? 'destructive' : 'primary'}
            loading={loading}
            onClick={onConfirm}
          >
            {loading ? `${confirmLabel}…` : confirmLabel}
          </Button>
        </>
      }
    />
  );
}

/* ------------------------------------------------------------ EntityDrawer */

export interface EntityDrawerProps {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  trigger?: React.ReactNode;
  title: string;
  loading?: boolean;
  error?: string | null;
  submitting?: boolean;
  dirty?: boolean;
  submitLabel?: string;
  onSubmit?: () => void;
  children?: React.ReactNode;
}

/** A Drawer specialized for entity create/edit forms with loading/error/submit states. */
export function EntityDrawer({
  open,
  onOpenChange,
  trigger,
  title,
  loading = false,
  error = null,
  submitting = false,
  dirty = false,
  submitLabel = 'Save',
  onSubmit,
  children,
}: EntityDrawerProps) {
  function handleOpenChange(next: boolean) {
    if (!next && dirty && !window.confirm('Discard unsaved changes?')) return;
    onOpenChange?.(next);
  }

  return (
    <Drawer
      open={open}
      onOpenChange={handleOpenChange}
      trigger={trigger}
      title={title}
      footer={
        <>
          <DialogPrimitive.Close asChild>
            <Button variant="secondary" disabled={submitting}>
              Cancel
            </Button>
          </DialogPrimitive.Close>
          <Button onClick={onSubmit} loading={submitting} disabled={!dirty || loading}>
            {submitLabel}
          </Button>
        </>
      }
    >
      {loading ? (
        <div data-testid="entity-drawer-loading" className="flex items-center gap-2 text-secondary text-neutral-500">
          <Spinner /> Loading…
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {error && (
            <Alert variant="error" title="Something went wrong">
              {error}
            </Alert>
          )}
          {children}
        </div>
      )}
    </Drawer>
  );
}

/* ----------------------------------------------------------- DropdownMenu */

export const DropdownMenu = DropdownMenuPrimitive.Root;
export const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger;

export const DropdownMenuContent = React.forwardRef<
  React.ElementRef<typeof DropdownMenuPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <DropdownMenuPrimitive.Portal>
    <DropdownMenuPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      collisionPadding={8}
      className={cn(
        // Bounded to the space Radix computes is actually free (flips/repositions near viewport
        // edges) so long menus scroll internally instead of clipping — scrollbar hidden but the
        // content stays scrollable (wheel/touch/keyboard nav all still work).
        'z-dropdown min-w-[10rem] max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-y-auto overflow-x-hidden rounded-md border border-default bg-neutral-0 p-1 shadow-md [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
        className,
      )}
      {...props}
    />
  </DropdownMenuPrimitive.Portal>
));
DropdownMenuContent.displayName = 'DropdownMenuContent';

export const DropdownMenuItem = React.forwardRef<
  React.ElementRef<typeof DropdownMenuPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Item> & { destructive?: boolean }
>(({ className, destructive, ...props }, ref) => (
  <DropdownMenuPrimitive.Item
    ref={ref}
    className={cn(
      'flex cursor-pointer select-none items-center rounded-sm px-2 py-1.5 text-body outline-none data-[highlighted]:bg-neutral-100',
      destructive ? 'text-danger-700' : 'text-neutral-800',
      className,
    )}
    {...props}
  />
));
DropdownMenuItem.displayName = 'DropdownMenuItem';

export const DropdownMenuSeparator = DropdownMenuPrimitive.Separator;

/* ---------------------------------------------------------------- Tooltip */

export interface TooltipProps {
  content: React.ReactNode;
  children: React.ReactNode;
}

export function Tooltip({ content, children }: TooltipProps) {
  return (
    <TooltipPrimitive.Provider delayDuration={200}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            sideOffset={4}
            className="z-toast rounded-md bg-black px-2 py-1 text-caption text-white shadow-md"
          >
            {content}
            <TooltipPrimitive.Arrow className="fill-black" />
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  );
}
