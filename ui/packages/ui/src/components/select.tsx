import * as Popover from '@radix-ui/react-popover';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from 'cmdk';
import { Check, ChevronDown } from 'lucide-react';
import * as React from 'react';
import { cn } from '../styles/cn';

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps {
  value?: string;
  onValueChange?: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
  className?: string;
}

/**
 * Token-styled single-select. Deliberately a native `<select>` rather than a
 * pointer-event-driven overlay (Radix Select opens only on `pointerdown`,
 * which this test environment's jsdom has no `PointerEvent` support for —
 * the interaction would silently never open). A native select needs no
 * pointer events, is trivially operable via `userEvent.selectOptions`, and
 * keyboard/native accessibility come for free.
 */
export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ value, onValueChange, options, placeholder = 'Select…', disabled, className, ...aria }, ref) => (
    <div className="relative">
      <select
        ref={ref}
        {...aria}
        value={value ?? ''}
        disabled={disabled}
        onChange={(e) => onValueChange?.(e.target.value)}
        className={cn(
          'h-9 w-full appearance-none rounded-md border border-default bg-neutral-0 px-3 pr-8 text-body focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 disabled:opacity-50',
          value ? 'text-neutral-900' : 'text-neutral-400',
          className,
        )}
      >
        <option value="" disabled hidden>
          {placeholder}
        </option>
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-500"
        aria-hidden="true"
      />
    </div>
  ),
);
Select.displayName = 'Select';

export interface ComboboxProps extends SelectProps {
  searchPlaceholder?: string;
  emptyText?: string;
}

/**
 * Searchable single-select (cmdk in a Radix Popover) for larger option sets.
 * ponytail: not yet exercised by any feature spec or test — if a future spec
 * needs it, verify it against this same jsdom (no PointerEvent) first; the
 * Popover trigger may share Select's pointerdown-open limitation.
 */
export function Combobox({
  value,
  onValueChange,
  options,
  placeholder = 'Select…',
  searchPlaceholder = 'Search…',
  emptyText = 'No results.',
  disabled,
  className,
  ...aria
}: ComboboxProps) {
  const [open, setOpen] = React.useState(false);
  const selected = options.find((o) => o.value === value);

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger
        {...aria}
        disabled={disabled}
        role="combobox"
        aria-expanded={open}
        className={cn(
          'inline-flex h-9 w-full items-center justify-between gap-2 rounded-md border border-default bg-neutral-0 px-3 text-body text-neutral-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600 disabled:opacity-50',
          className,
        )}
      >
        <span className={cn(!selected && 'text-neutral-400')}>{selected ? selected.label : placeholder}</span>
        <ChevronDown className="h-4 w-4 text-neutral-500" aria-hidden="true" />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          className="z-dropdown w-[var(--radix-popover-trigger-width)] overflow-hidden rounded-md border border-default bg-neutral-0 shadow-md"
        >
          <Command className="flex flex-col">
            <CommandInput
              placeholder={searchPlaceholder}
              className="h-9 border-b border-subtle bg-transparent px-3 text-body outline-none placeholder:text-neutral-400"
            />
            <CommandList className="max-h-60 overflow-y-auto p-1">
              <CommandEmpty className="px-2 py-3 text-secondary text-neutral-500">{emptyText}</CommandEmpty>
              <CommandGroup>
                {options.map((opt) => (
                  <CommandItem
                    key={opt.value}
                    value={opt.label}
                    onSelect={() => {
                      onValueChange?.(opt.value);
                      setOpen(false);
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-sm px-2 py-1.5 text-body text-neutral-800 data-[selected=true]:bg-neutral-100"
                  >
                    {opt.label}
                    {value === opt.value && <Check className="h-4 w-4 text-primary-600" aria-hidden="true" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
