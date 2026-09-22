import { request } from './client';
import type { Permission } from './types';

export function listPermissions(accessToken?: string | null): Promise<Permission[]> {
  return request<Permission[]>('/permissions', { accessToken });
}

export interface PermissionInput {
  resource: string;
  action: string;
  description?: string;
}

/**
 * Mutating: gated by permissions.create. The mock backend checks a permission header
 * independent of what the UI displayed (§74, §230) — the same pattern as org-portal's
 * createUser/useCreateUser, not a demo kept separate from the real flow.
 */
export function createMasterPermission(
  input: PermissionInput,
  mockPermissions: string[] = [],
  accessToken?: string | null,
): Promise<Permission> {
  return request<Permission>('/permissions', { method: 'POST', body: input, mockPermissions, accessToken });
}

export function updatePermission(
  id: string,
  input: Partial<PermissionInput>,
  accessToken?: string | null,
): Promise<Permission> {
  return request<Permission>(`/permissions/${id}`, { method: 'PATCH', body: input, accessToken });
}

/** Soft-deactivate only — there is no delete export; never a hard delete (§52). */
export function deactivatePermission(id: string, accessToken?: string | null): Promise<Permission> {
  return request<Permission>(`/permissions/${id}`, {
    method: 'PATCH',
    body: { is_active: false },
    accessToken,
  });
}

export function activatePermission(id: string, accessToken?: string | null): Promise<Permission> {
  return request<Permission>(`/permissions/${id}`, {
    method: 'PATCH',
    body: { is_active: true },
    accessToken,
  });
}
