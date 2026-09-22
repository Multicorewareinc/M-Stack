import { useQueryClient } from '@tanstack/react-query';
import * as React from 'react';
import { setCurrentMockOrgId } from '../mocks/mockOrgContext';
import { AuthContext, type AuthUser, type OrganizationContext } from './AuthContext';
import { setCurrentOrgId } from './currentOrgId';
import { shouldUseMockAuth } from './env';

const DEFAULT_USER: AuthUser = { id: 'org_admin_1', displayName: 'Org Admin', role: 'org_admin' };
// VITE_DEV_ORG_ID overrides which org this points at — set it to a real org's UUID when running
// against docker-compose.rbac.yml (org_001 is a mock-only id the real backend would reject).
const DEFAULT_ORG: OrganizationContext = {
  id: import.meta.env.VITE_DEV_ORG_ID || 'org_001',
  name: 'Acme Corporation',
};

// Default org-scope permissions for the Org Admin persona (mock only — see
// admin-portal's MockAuthProvider for the same rationale, ADR-023).
const DEFAULT_PERMISSIONS = [
  'users.read', 'users.create', 'users.update',
  'roles.read', 'roles.create', 'roles.update',
];

export interface MockAuthProviderProps {
  children: React.ReactNode;
  /** Override the mock permission set — used by tests to exercise denied states. */
  permissions?: string[];
  /** Override the mocked organization context — used by tests to simulate a tenant switch. */
  organizationContext?: OrganizationContext;
}

/**
 * Temporary auth provider (ADR-023). Never enable this outside dev/test —
 * main.tsx gates its construction behind import.meta.env.DEV || MODE==='test'.
 * Tenant-sensitive cache eviction on organization switch is handled by
 * useTenantCacheEviction (see tenantCache.ts), wired here.
 */
export function MockAuthProvider({
  children,
  permissions = DEFAULT_PERMISSIONS,
  organizationContext: organizationContextProp = DEFAULT_ORG,
}: MockAuthProviderProps) {
  const queryClient = useQueryClient();
  const [organizationContext, setOrganizationContext] = React.useState(organizationContextProp);
  const previousOrgId = React.useRef(organizationContext.id);

  // A caller-supplied organizationContext (tests simulate a tenant switch by passing a new
  // object on rerender) always wins over any live switch made below.
  React.useEffect(() => {
    setOrganizationContext(organizationContextProp);
  }, [organizationContextProp]);

  React.useEffect(() => {
    if (previousOrgId.current !== organizationContext.id) {
      // Evict every tenant-sensitive query for the previous organization so
      // its data can never render under the new tenant context (§174-175).
      queryClient.removeQueries({
        predicate: (query) => query.queryKey.includes(previousOrgId.current),
      });
      previousOrgId.current = organizationContext.id;
    }
    setCurrentMockOrgId(organizationContext.id);
    setCurrentOrgId(organizationContext.id);
  }, [organizationContext.id, queryClient]);

  // Test-only identity-switch seam (SP-13 tenant-isolation hardening): lets Playwright trigger a
  // live org switch — no full page reload — via window.__setMockOrg, exercising the same
  // cache-eviction effect above a real org switch would go through. Never exposed in production.
  React.useEffect(() => {
    if (!shouldUseMockAuth()) return;
    (window as unknown as { __setMockOrg?: (ctx: OrganizationContext) => void }).__setMockOrg = setOrganizationContext;
    return () => {
      delete (window as unknown as { __setMockOrg?: (ctx: OrganizationContext) => void }).__setMockOrg;
    };
  }, []);

  const value = React.useMemo(
    () => ({
      user: DEFAULT_USER,
      organizationContext,
      permissions,
      getCurrentUser: () => DEFAULT_USER,
      getOrganizationContext: () => organizationContext,
      // Bridges to a real backend under docker-compose.rbac.yml, which gates every route on a
      // shared bearer key (see that file's header comment) — set VITE_DEV_API_KEY to match.
      // Unset (MSW-mocked mode) => no header, same as before. Throwaway once real auth ships.
      getAccessToken: () => import.meta.env.VITE_DEV_API_KEY || null,
      logout: () => {
        queryClient.clear();
      },
    }),
    [organizationContext, permissions, queryClient],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
