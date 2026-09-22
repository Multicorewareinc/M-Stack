import { EmptyState } from '@multistack/ui';

export interface SessionExpiredProps {
  onBack?: () => void;
}

/** 401 state. Distinct from 403 (access denied) — never redirects to login (§89-90). */
export function SessionExpired({ onBack }: SessionExpiredProps) {
  return (
    <EmptyState
      title="Session expired"
      description="Your session has expired. Please sign in again."
      action={onBack ? { label: 'Back', onClick: onBack } : undefined}
    />
  );
}
