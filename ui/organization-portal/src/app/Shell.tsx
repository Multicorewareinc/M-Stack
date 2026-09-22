import { Outlet } from 'react-router-dom';
import { Header } from '../components/layout/Header';
import { Sidebar } from '../components/layout/Sidebar';
import { AccessDenied } from './AccessDenied';
import { useGlobalErrorView } from './QueryProvider';
import { SessionExpired } from './SessionExpired';

export function Shell() {
  const errorView = useGlobalErrorView();

  return (
    <div className="flex h-screen flex-col">
      <Header />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <main className="flex-1 overflow-y-auto p-6">
          {errorView === 'access-denied' && <AccessDenied />}
          {errorView === 'session-expired' && <SessionExpired />}
          {!errorView && <Outlet />}
        </main>
      </div>
    </div>
  );
}
