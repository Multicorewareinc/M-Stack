import { request } from './client';
import type { Role, RolePermission } from './types';

export function listRoles(accessToken?: string | null): Promise<Role[]> {
  return request<Role[]>('/roles', { accessToken });
}

export function getRole(id: string, accessToken?: string | null): Promise<Role> {
  return request<Role>(`/roles/${id}`, { accessToken });
}

export interface RoleInput {
  name: string;
  description?: string;
}

export function createRole(input: RoleInput, accessToken?: string | null): Promise<Role> {
  return request<Role>('/roles', { method: 'POST', body: input, accessToken });
}

export function updateRole(id: string, input: Partial<RoleInput>, accessToken?: string | null): Promise<Role> {
  return request<Role>(`/roles/${id}`, { method: 'PATCH', body: input, accessToken });
}

/** May reject with CONFLICT if the role is assigned to users (§112) — never assumed client-side. */
export function deleteRole(id: string, accessToken?: string | null): Promise<void> {
  return request<void>(`/roles/${id}`, { method: 'DELETE', accessToken });
}

export function getRolePermissions(id: string, accessToken?: string | null): Promise<RolePermission> {
  return request<RolePermission>(`/roles/${id}/permissions`, { accessToken });
}

export function setRolePermissions(
  id: string,
  permissionIds: string[],
  accessToken?: string | null,
): Promise<RolePermission> {
  return request<RolePermission>(`/roles/${id}/permissions`, {
    method: 'PUT',
    body: { permission_ids: permissionIds },
    accessToken,
  });
}
