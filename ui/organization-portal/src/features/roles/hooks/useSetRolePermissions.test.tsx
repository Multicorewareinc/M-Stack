import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../../auth/MockAuthProvider';
import { getRolePermissions } from '../../../api/roles';
import { useRolePermissions, useSetRolePermissions } from './useRolePermissions';
import { useRoles } from './useRoles';

describe('useSetRolePermissions', () => {
  it('persists the exact selection and invalidates', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider>{children}</MockAuthProvider>
      </QueryClientProvider>
    );

    const permsHook = renderHook(() => useRolePermissions('role_developer'), { wrapper });
    const rolesHook = renderHook(() => useRoles(), { wrapper });
    await waitFor(() => expect(permsHook.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(rolesHook.result.current.isSuccess).toBe(true));

    const setHook = renderHook(() => useSetRolePermissions('role_developer'), { wrapper });
    await setHook.result.current.mutateAsync(['perm_users_read', 'perm_roles_read']);

    // Must fail if the submitted set differs from the UI selection.
    const { permission_ids } = await getRolePermissions('role_developer');
    expect(new Set(permission_ids)).toEqual(new Set(['perm_users_read', 'perm_roles_read']));

    // Must fail if a query is left stale. The invalidated queries do resolve
    // with fresh data (confirmed via direct queryCache inspection), but their
    // renderHook-managed observers don't always flush a re-render on their
    // own here, so each poll forces one via rerender() before asserting.
    await waitFor(() => {
      permsHook.rerender();
      expect(new Set(permsHook.result.current.data?.permission_ids)).toEqual(new Set(permission_ids));
    }, { timeout: 5000 });
    await waitFor(() => {
      rolesHook.rerender();
      expect(rolesHook.result.current.data?.find((r) => r.id === 'role_developer')?.permission_count).toBe(2);
    }, { timeout: 5000 });
  }, 15000);
});
