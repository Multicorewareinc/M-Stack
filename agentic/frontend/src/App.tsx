import * as React from 'react';
import { Header } from './components/layout/Header';
import { Sidebar } from './components/layout/Sidebar';
import { CapabilitiesPage } from './features/plan/CapabilitiesPage';
import { HistoryPage } from './features/plan/HistoryPage';
import { PlanPage } from './features/plan/PlanPage';

export function App() {
  const [page, setPage] = React.useState('plan');
  const [openSessionId, setOpenSessionId] = React.useState<string | null>(null);
  // Bumped to force PlanPage to remount -- the one place its internal
  // conversation state should actually be thrown away, as opposed to
  // switching tabs and back, which keeps it (see the hidden div below).
  const [planKey, setPlanKey] = React.useState(0);

  function startNewPlan() {
    setOpenSessionId(null);
    setPlanKey((k) => k + 1);
    setPage('plan');
  }

  function openFromHistory(sessionId: string) {
    setOpenSessionId(sessionId);
    setPlanKey((k) => k + 1);
    setPage('plan');
  }

  return (
    <div className="flex h-screen flex-col bg-neutral-50">
      <Header />
      <div className="flex min-h-0 flex-1">
        <Sidebar active={page} onSelect={setPage} />
        <main className="flex min-h-0 flex-1 flex-col">
          {/* PlanPage stays mounted even while another tab is showing, so
              switching to Capabilities/History and back doesn't lose an
              in-progress conversation -- only "New plan" and opening a
              saved one (both bump planKey) actually reset it. */}
          <div className={page === 'plan' ? 'flex min-h-0 flex-1 flex-col' : 'hidden'}>
            <PlanPage key={planKey} initialSessionId={openSessionId} onNewPlan={startNewPlan} />
          </div>
          {page === 'capabilities' && <CapabilitiesPage />}
          {page === 'history' && <HistoryPage onOpen={openFromHistory} />}
        </main>
      </div>
    </div>
  );
}
