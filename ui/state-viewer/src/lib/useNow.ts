import { useQueryClient } from '@tanstack/react-query';
import * as React from 'react';

const TICK_MS = 15_000;

/**
 * The current time, for "5 min ago" labels. A relative time is derived from the clock, not from the
 * data, so it goes stale while the rows themselves are unchanged (react-query keeps the same objects
 * when a refetch returns identical data, so nothing re-renders). This re-renders on a slow tick, and
 * again whenever any query finishes fetching — so pressing Refresh visibly updates the labels even if
 * the database did not change.
 */
export function useNow(): number {
  const queryClient = useQueryClient();
  const [now, setNow] = React.useState(() => Date.now());

  React.useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), TICK_MS);
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      if (event.type === 'updated' && event.action.type === 'success') setNow(Date.now());
    });
    return () => {
      clearInterval(tick);
      unsubscribe();
    };
  }, [queryClient]);

  return now;
}
