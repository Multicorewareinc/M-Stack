import { EmptyState, PageHeader } from '@multistack/ui';

/**
 * Settings placeholder (A-16, blueprint §207 P2). Explicitly labeled so it
 * never reads as a broken route — a real screen is out of scope for V1.
 */
export function Settings() {
  return (
    <div>
      <PageHeader eyebrow="Platform Control Plane / Settings" title="Settings" />
      <EmptyState title="Settings coming soon" description="Platform settings are not yet available." />
    </div>
  );
}
