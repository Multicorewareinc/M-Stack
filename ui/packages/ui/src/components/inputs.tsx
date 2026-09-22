import * as RadioGroup from '@radix-ui/react-radio-group';
import * as SwitchPrimitive from '@radix-ui/react-switch';
import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import * as LabelPrimitive from '@radix-ui/react-label';
import { cva, type VariantProps } from 'class-variance-authority';
import { Check, Loader2 } from 'lucide-react';
import * as React from 'react';
import { cn } from '../styles/cn';

/* ---------------------------------------------------------------- Button */

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-body font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        primary: 'bg-primary-600 text-white hover:bg-primary-700',
        secondary: 'border border-default bg-neutral-0 text-neutral-800 hover:bg-neutral-50',
        ghost: 'text-neutral-700 hover:bg-neutral-100',
        destructive: 'bg-danger-500 text-white hover:bg-danger-700',
      },
      size: {
        sm: 'h-8 px-3',
        md: 'h-9 px-4',
        lg: 'h-10 px-5',
        icon: 'h-9 w-9',
      },
    },
    defaultVariants: { variant: 'primary', size: 'md' },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading = false, disabled, children, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
      {children}
    </button>
  ),
);
Button.displayName = 'Button';

export interface IconButtonProps extends ButtonProps {
  'aria-label': string;
}

export const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ size = 'icon', ...props }, ref) => <Button ref={ref} size={size} {...props} />,
);
IconButton.displayName = 'IconButton';

/* ----------------------------------------------------------------- Label */

export const Label = React.forwardRef<
  React.ElementRef<typeof LabelPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof LabelPrimitive.Root>
>(({ className, ...props }, ref) => (
  <LabelPrimitive.Root
    ref={ref}
    className={cn('text-secondary font-medium text-neutral-700', className)}
    {...props}
  />
));
Label.displayName = 'Label';

/* ----------------------------------------------------------------- Input */

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  error?: boolean;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, error, ...props }, ref) => (
    <input
      ref={ref}
      aria-invalid={error || undefined}
      className={cn(
        'flex h-9 w-full rounded-md border border-default bg-neutral-0 px-3 text-body text-neutral-900 placeholder:text-neutral-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 disabled:opacity-50',
        error && 'border-danger-500 focus-visible:ring-danger-500',
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = 'Input';

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: boolean;
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, error, ...props }, ref) => (
    <textarea
      ref={ref}
      aria-invalid={error || undefined}
      className={cn(
        'flex min-h-[72px] w-full rounded-md border border-default bg-neutral-0 px-3 py-2 text-body text-neutral-900 placeholder:text-neutral-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 disabled:opacity-50',
        error && 'border-danger-500 focus-visible:ring-danger-500',
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = 'Textarea';

/* -------------------------------------------------------------- Checkbox */

export const Checkbox = React.forwardRef<
  React.ElementRef<typeof CheckboxPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root>
>(({ className, ...props }, ref) => (
  <CheckboxPrimitive.Root
    ref={ref}
    className={cn(
      'peer h-4 w-4 shrink-0 rounded-sm border border-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 disabled:opacity-50 data-[state=checked]:border-primary-600 data-[state=checked]:bg-primary-600 data-[state=checked]:text-white',
      className,
    )}
    {...props}
  >
    <CheckboxPrimitive.Indicator className="flex items-center justify-center">
      <Check className="h-3.5 w-3.5" aria-hidden="true" />
    </CheckboxPrimitive.Indicator>
  </CheckboxPrimitive.Root>
));
Checkbox.displayName = 'Checkbox';

/* ---------------------------------------------------------------- Switch */

export const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      'peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 disabled:opacity-50 data-[state=checked]:bg-primary-600 data-[state=unchecked]:bg-neutral-300',
      className,
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb className="pointer-events-none block h-4 w-4 rounded-full bg-neutral-0 shadow-sm transition-transform data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0.5" />
  </SwitchPrimitive.Root>
));
Switch.displayName = 'Switch';

/* ----------------------------------------------------------------- Radio */

export const Radio = RadioGroup.Root;

export const RadioItem = React.forwardRef<
  React.ElementRef<typeof RadioGroup.Item>,
  React.ComponentPropsWithoutRef<typeof RadioGroup.Item>
>(({ className, ...props }, ref) => (
  <RadioGroup.Item
    ref={ref}
    className={cn(
      'aspect-square h-4 w-4 rounded-full border border-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 disabled:opacity-50 data-[state=checked]:border-primary-600',
      className,
    )}
    {...props}
  >
    <RadioGroup.Indicator className="relative flex h-full w-full items-center justify-center after:block after:h-2 after:w-2 after:rounded-full after:bg-primary-600" />
  </RadioGroup.Item>
));
RadioItem.displayName = 'RadioItem';

/* ------------------------------------------------------- FormField/Section */

export interface FormFieldProps {
  label: string;
  htmlFor: string;
  error?: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
  className?: string;
}

/**
 * Associates a label, control, and error so the control is programmatically
 * labeled. Consumers give their control `id={htmlFor}` and spread the returned
 * describedById via aria-describedby when they need custom controls; the common
 * case (a single labeled child) is wired automatically.
 */
export function FormField({
  label,
  htmlFor,
  error,
  hint,
  required,
  children,
  className,
}: FormFieldProps) {
  const errorId = `${htmlFor}-error`;
  const hintId = `${htmlFor}-hint`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ') || undefined;

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      {/* The required asterisk is a sibling of <label>, not a child of it —
          keeping it inside would leak into the label's plain-textContent
          accessible name (e.g. "Organization name*"), breaking exact-match
          getByLabelText('Organization name') queries for every required field. */}
      <span className="inline-flex items-baseline gap-0.5">
        <Label htmlFor={htmlFor}>{label}</Label>
        {required && (
          <span className="text-danger-500" aria-hidden="true">
            *
          </span>
        )}
      </span>
      {React.isValidElement(children)
        ? React.cloneElement(children as React.ReactElement, {
            id: htmlFor,
            'aria-describedby': describedBy,
            'aria-invalid': error ? true : undefined,
          })
        : children}
      {hint && !error && (
        <p id={hintId} className="text-caption text-neutral-500">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-caption text-danger-700">
          {error}
        </p>
      )}
    </div>
  );
}

export interface FormSectionProps {
  title: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}

export function FormSection({ title, description, children, className }: FormSectionProps) {
  return (
    <section className={cn('flex flex-col gap-4 border-t border-subtle pt-4 first:border-t-0 first:pt-0', className)}>
      <div className="flex flex-col gap-1">
        <h3 className="text-cardTitle font-semibold text-neutral-900">{title}</h3>
        {description && <p className="text-secondary text-neutral-500">{description}</p>}
      </div>
      <div className="flex flex-col gap-4">{children}</div>
    </section>
  );
}
