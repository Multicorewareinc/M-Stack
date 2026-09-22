import { Avatar } from '@multistack/ui';

export function Header() {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-subtle bg-neutral-0 px-4">
      <span className="text-body font-semibold text-neutral-900">Multistack</span>
      <div className="flex items-center gap-2 rounded-md px-2 py-1">
        <Avatar name="test" />
        <span className="text-body text-neutral-700">test</span>
      </div>
    </header>
  );
}
