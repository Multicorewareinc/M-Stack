import { Alert, EmptyState, Skeleton } from '@multistack/ui';
import type { ReactNode } from 'react';
import { useInfo } from './hooks';

/**
 * Wraps every page. Three states are not "a page with no rows" and deserve their own message:
 * the API is not running, the API is up but cannot read the database, and the database does not
 * exist yet (a machine that has not deployed anything — the normal first-run state).
 */
export function StateGate({ children }: { children: ReactNode }) {
  const { data: info, error, isLoading, refetch } = useInfo();

  if (isLoading) return <Skeleton className="h-24 w-full" />;

  if (error) {
    return (
      <Alert variant="error" title="Cannot read the state database">
        <p>{error.message}</p>
        <button className="mt-2 underline" onClick={() => refetch()}>
          Try again
        </button>
      </Alert>
    );
  }

  if (info && !info.exists) {
    return (
      <EmptyState
        title="No state database yet"
        description={`Nothing has been deployed with the SDK on this machine (looked for ${info.db_path}). Run a script that creates a cluster or capability and it will appear here. To read a different file, start the viewer with --db or set MULTISTACK_STATE_DB.`}
      />
    );
  }

  return <>{children}</>;
}
