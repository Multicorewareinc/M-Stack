import { DetailDialog } from '@multistack/ui';
import { useLocation, useNavigate, Route, Routes, type Location } from 'react-router-dom';
import { ApiKeysPage } from '../features/apiKeys/ApiKeysPage';
import { BillingPage } from '../features/billing/BillingPage';
import { ChatPage } from '../features/chat/ChatPage';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { PermissionsPage } from '../features/permissions/PermissionsPage';
import { RoleDetailPage } from '../features/roles/RoleDetailPage';
import { RolesPage } from '../features/roles/RolesPage';
import { SettingsPage } from '../features/settings/SettingsPage';
import { UserDetailPage } from '../features/users/UserDetailPage';
import { UsersPage } from '../features/users/UsersPage';
import { NotFound } from '../pages/NotFound';
import { Shell } from './Shell';

interface NavState {
  backgroundLocation?: Location;
}

/**
 * Detail routes (user/role) render as a popup over the list they were opened
 * from when reached via an in-app row click (which stashes the list's
 * location in nav state), and as a normal full page on direct navigation or
 * refresh (no stashed state to fall back on) — same detail components either
 * way, just composed differently.
 */
export function AppRoutes() {
  const location = useLocation();
  const navigate = useNavigate();
  const state = location.state as NavState | null;
  const backgroundLocation = state?.backgroundLocation;

  function closeDetail() {
    navigate(-1);
  }

  return (
    <>
      <Routes location={backgroundLocation ?? location}>
        <Route element={<Shell />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/users/:userId" element={<UserDetailPage />} />
          <Route path="/roles" element={<RolesPage />} />
          <Route path="/roles/:roleId" element={<RoleDetailPage />} />
          <Route path="/permissions" element={<PermissionsPage />} />
          <Route path="/api-keys" element={<ApiKeysPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/billing" element={<BillingPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
      {backgroundLocation && (
        <Routes>
          <Route
            path="/users/:userId"
            element={
              <DetailDialog open label="User details" onOpenChange={closeDetail}>
                <UserDetailPage />
              </DetailDialog>
            }
          />
          <Route
            path="/roles/:roleId"
            element={
              <DetailDialog open label="Role details" onOpenChange={closeDetail}>
                <RoleDetailPage />
              </DetailDialog>
            }
          />
        </Routes>
      )}
    </>
  );
}
