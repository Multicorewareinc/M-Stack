import { request } from './client';
import type { User } from './types';

export function listUsers(accessToken?: string | null): Promise<User[]> {
  return request<User[]>('/users', { accessToken });
}

export function getUser(id: string, accessToken?: string | null): Promise<User> {
  return request<User>(`/users/${id}`, { accessToken });
}

export interface CreateUserInput {
  username: string;
  email: string;
  first_name?: string;
  last_name?: string;
  display_name?: string;
}

/** Mutating: gated by users.create. The mock backend checks a permission
 * header independent of what the UI displayed (§74, §230). Routed through the shared request()
 * helper (like every other call here) so it gets the same X-Organization-Id header and
 * VITE_DEV_API_KEY fallback as everything else — a hand-rolled fetch used to bypass both. */
export function createUser(input: CreateUserInput, permissions: string[] = []): Promise<User> {
  return request<User>('/users', { method: 'POST', body: input, mockPermissions: permissions });
}

export interface UpdateUserInput {
  email?: string;
  first_name?: string;
  last_name?: string;
  display_name?: string;
  status?: string;
  // Admin-initiated password reset (no current_password — that's only required for a user
  // changing their OWN password via POST /api/auth/password). The backend hashes it and forces
  // rotation on the user's next login (must_change_password=True), same as user creation.
  password?: string;
}

/** Profile fields only — never the immutable username/id (§59). */
export function updateUser(id: string, input: UpdateUserInput, accessToken?: string | null): Promise<User> {
  return request<User>(`/users/${id}`, { method: 'PATCH', body: input, accessToken });
}

export function getUserRoles(id: string, accessToken?: string | null): Promise<{ role_ids: string[] }> {
  return request<{ role_ids: string[] }>(`/users/${id}/roles`, { accessToken });
}

/** Role assignment is a distinct operation from create/edit (§60) — submits the full new role-id set. */
export function setUserRoles(
  id: string,
  roleIds: string[],
  accessToken?: string | null,
): Promise<{ role_ids: string[] }> {
  return request<{ role_ids: string[] }>(`/users/${id}/roles`, {
    method: 'PUT',
    body: { role_ids: roleIds },
    accessToken,
  });
}

/** Computed by Org CP through role composition (§108) — never client-computed. */
export function getUserEffectivePermissions(
  id: string,
  accessToken?: string | null,
): Promise<{ permission_ids: string[] }> {
  return request<{ permission_ids: string[] }>(`/users/${id}/permissions`, { accessToken });
}

export function deleteUser(id: string, accessToken?: string | null): Promise<void> {
  return request<void>(`/users/${id}`, { method: 'DELETE', accessToken });
}
