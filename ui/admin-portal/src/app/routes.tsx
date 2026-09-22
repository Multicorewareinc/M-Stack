import { DetailDialog } from '@multistack/ui';
import { useLocation, useNavigate, Route, Routes, type Location } from 'react-router-dom';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { OrganizationDetailPage } from '../features/organizations/OrganizationDetailPage';
import { OrganizationsPage } from '../features/organizations/OrganizationsPage';
import { PlansPage } from '../features/plans/PlansPage';
import { PermissionsPage } from '../features/permissions/PermissionsPage';
import { UserDetailPage } from '../features/users/UserDetailPage';
import { UsersDirectoryPage } from '../features/users/UsersDirectoryPage';
import { NotFound } from '../pages/NotFound';
import { Settings } from '../pages/Settings';
import { Shell } from './Shell';

interface NavState {
  backgroundLocation?: Location;
}

/**
 * Detail routes (org/user) render as a popup over the list they were opened
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
          <Route path="/organizations" element={<OrganizationsPage />} />
          <Route path="/organizations/:id" element={<OrganizationDetailPage />} />
          <Route path="/plans" element={<PlansPage />} />
          <Route path="/permissions" element={<PermissionsPage />} />
          <Route path="/users" element={<UsersDirectoryPage />} />
          <Route path="/users/:userId" element={<UserDetailPage />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
      {backgroundLocation && (
        <Routes>
          <Route
            path="/organizations/:id"
            element={
              <DetailDialog open label="Organization details" onOpenChange={closeDetail}>
                <OrganizationDetailPage />
              </DetailDialog>
            }
          />
          <Route
            path="/users/:userId"
            element={
              <DetailDialog open label="User details" onOpenChange={closeDetail}>
                <UserDetailPage />
              </DetailDialog>
            }
          />
        </Routes>
      )}
    </>
  );
}
