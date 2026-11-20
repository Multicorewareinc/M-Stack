import * as React from 'react';

/** Auto-refresh cadence in ms, or false for manual only. The state db is written by a separate
 * process (an SDK script), so the page polls rather than waiting to be told. */
export type RefreshInterval = number | false;

export const REFRESH_OPTIONS: { value: string; label: string; interval: RefreshInterval }[] = [
  { value: 'off', label: 'Auto-refresh off', interval: false },
  { value: '5s', label: 'Every 5 s', interval: 5_000 },
  { value: '15s', label: 'Every 15 s', interval: 15_000 },
  { value: '60s', label: 'Every minute', interval: 60_000 },
];

interface RefreshState {
  value: string;
  interval: RefreshInterval;
  setValue: (value: string) => void;
}

const RefreshContext = React.createContext<RefreshState>({ value: 'off', interval: false, setValue: () => {} });

export function RefreshProvider({ children, initial = '5s' }: { children: React.ReactNode; initial?: string }) {
  const [value, setValue] = React.useState(initial);
  const state = React.useMemo<RefreshState>(
    () => ({ value, setValue, interval: REFRESH_OPTIONS.find((o) => o.value === value)?.interval ?? false }),
    [value],
  );
  return <RefreshContext.Provider value={state}>{children}</RefreshContext.Provider>;
}

export const useRefresh = () => React.useContext(RefreshContext);
