import { EmptyState } from '@multistack/ui';

export interface AccessDeniedProps {
  onBack?: () => void;
}

/** 403 state. Distinct from 401 (session expired) — never redirects to login (§89-90). */
export function AccessDenied({ onBack }: AccessDeniedProps) {
  return (
    <EmptyState
      title="Access denied"
      description="You do not have permission to access this resource."
      action={onBack ? { label: 'Back', onClick: onBack } : undefined}
    />
  );
}
