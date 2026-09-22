import { cn } from '@multistack/ui';

export interface NavItem {
  label: string;
  id: string;
}

export const AGENTIC_NAV: NavItem[] = [
  { label: 'Build a plan', id: 'plan' },
  { label: 'Capabilities', id: 'capabilities' },
  { label: 'History', id: 'history' },
];

export function Sidebar({ active, onSelect }: { active: string; onSelect: (id: string) => void }) {
  return (
    <nav aria-label="Primary" className="flex w-56 shrink-0 flex-col gap-1 border-r border-subtle bg-neutral-0 p-3">
      <p className="px-2 pb-1 text-caption font-semibold uppercase tracking-wide text-neutral-500">Multistack</p>
      <p className="px-2 pb-2 text-body font-medium text-neutral-700">agentic</p>
      {AGENTIC_NAV.map((item) => (
        <button
          key={item.id}
          type="button"
          aria-current={active === item.id ? 'page' : undefined}
          onClick={() => onSelect(item.id)}
          className={cn(
            'rounded-md px-2 py-1.5 text-left text-body font-medium text-neutral-600 hover:bg-neutral-100',
            active === item.id && 'border-l-2 border-primary-600 bg-primary-50 font-semibold text-primary-700',
          )}
        >
          {item.label}
        </button>
      ))}
      <div className="mt-auto border-t border-subtle px-2 pt-3">
        <p className="text-caption text-neutral-500">
          Read-only by construction. This console can generate a script; it cannot run one.
        </p>
      </div>
    </nav>
  );
}
