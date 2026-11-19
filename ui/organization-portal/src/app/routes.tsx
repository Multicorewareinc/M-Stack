import { DetailDialog } from '@multistack/ui';
import { useLocation, useNavigate, Route, Routes, type Location } from 'react-router-dom';
import { ApiKeysPage } from '../features/apiKeys/ApiKeysPage';
import { AuditLogsPage } from '../features/auditLogs/AuditLogsPage';
import { BillingPage } from '../features/billing/BillingPage';
import { ChatPage } from '../features/chat/ChatPage';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { InvitationsPage } from '../features/invitations/InvitationsPage';
import { PermissionsPage } from '../features/permissions/PermissionsPage';
import { RoleDetailPage } from '../features/roles/RoleDetailPage';
import { RolesPage } from '../features/roles/RolesPage';
import { SettingsPage } from '../features/settings/SettingsPage';
import { UserDetailPage } from '../features/users/UserDetailPage';
import { UsersPage } from '../features/users/UsersPage';
import { NotFound } from '../pages/NotFound';
import { RequirePermission } from '../components/RequirePermission';
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
          <Route
            path="/users"
            element={
              <RequirePermission permission="users.read">
                <UsersPage />
              </RequirePermission>
            }
          />
          <Route
            path="/users/:userId"
            element={
              <RequirePermission permission="users.read">
                <UserDetailPage />
              </RequirePermission>
            }
          />
          <Route
            path="/invitations"
            element={
              <RequirePermission permission="users.create">
                <InvitationsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/roles"
            element={
              <RequirePermission permission="roles.read">
                <RolesPage />
              </RequirePermission>
            }
          />
          <Route
            path="/roles/:roleId"
            element={
              <RequirePermission permission="roles.read">
                <RoleDetailPage />
              </RequirePermission>
            }
          />
          <Route
            path="/permissions"
            element={
              // No dedicated "permissions.read" slug exists in the master catalog — gated on
              // roles.read since browsing the catalog is part of the same role-composition
              // capability (anyone who can view roles can see what they're composed from).
              <RequirePermission permission="roles.read">
                <PermissionsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/api-keys"
            element={
              <RequirePermission permission={['apikey.create', 'apikey.update', 'apikey.delete']}>
                <ApiKeysPage />
              </RequirePermission>
            }
          />
          <Route path="/audit-logs" element={<AuditLogsPage />} />
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
              <RequirePermission permission="users.read">
                <DetailDialog open label="User details" onOpenChange={closeDetail}>
                  <UserDetailPage />
                </DetailDialog>
              </RequirePermission>
            }
          />
          <Route
            path="/roles/:roleId"
            element={
              <RequirePermission permission="roles.read">
                <DetailDialog open label="Role details" onOpenChange={closeDetail}>
                  <RoleDetailPage />
                </DetailDialog>
              </RequirePermission>
            }
          />
        </Routes>
      )}
    </>
  );
}
