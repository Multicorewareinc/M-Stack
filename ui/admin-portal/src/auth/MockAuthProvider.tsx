import { useQueryClient } from '@tanstack/react-query';
import * as React from 'react';
import { AuthContext, type AuthUser } from './AuthContext';

const DEFAULT_USER: AuthUser = { id: 'super_admin_1', displayName: 'Super Admin', role: 'super_admin' };

// All platform-scope permissions the Super Admin persona can carry. A real
// identity provider will supply the actual grant set once the auth module
// ships (Architecture §44); this mock exists only so RBAC-visibility UX is
// buildable and testable now (ADR-023).
const DEFAULT_PERMISSIONS = [
  'organizations.read', 'organizations.create', 'organizations.update',
  'plans.read', 'plans.create', 'plans.update',
  'permissions.read', 'permissions.create', 'permissions.update',
  'users.read',
];

export interface MockAuthProviderProps {
  children: React.ReactNode;
  /** Override the mock permission set — used by tests to exercise denied states. */
  permissions?: string[];
}

/**
 * Temporary auth provider (ADR-023). Never enable this outside dev/test —
 * main.tsx gates its construction behind import.meta.env.DEV || MODE==='test'.
 */
export function MockAuthProvider({ children, permissions = DEFAULT_PERMISSIONS }: MockAuthProviderProps) {
  const queryClient = useQueryClient();

  const value = React.useMemo(
    () => ({
      user: DEFAULT_USER,
      permissions,
      getCurrentUser: () => DEFAULT_USER,
      // Bridges to a real backend under docker-compose.rbac.yml, which gates every route on a
      // shared bearer key (see that file's header comment) — set VITE_DEV_API_KEY to match.
      // Unset (MSW-mocked mode) => no header, same as before. Throwaway once real auth ships.
      getAccessToken: () => import.meta.env.VITE_DEV_API_KEY || null,
      logout: () => {
        queryClient.clear();
      },
    }),
    [permissions, queryClient],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
