import { request } from './client';
import type { UserDetail, UserDirectoryEntry } from './types';

export function listUserDirectory(accessToken?: string | null): Promise<UserDirectoryEntry[]> {
  return request<UserDirectoryEntry[]>('/users', { accessToken });
}

export function getUserDetail(id: string, accessToken?: string | null): Promise<UserDetail> {
  return request<UserDetail>(`/users/${id}`, { accessToken });
}

export interface CreateUserInput {
  username: string;
  email: string;
  /** Temporary password — the backend hashes it, sets must_change_password=true, and the user
   * is forced through the reset-password flow on their first org-portal login (ADR-029/AD-02b,c). */
  password: string;
  display_name?: string;
}

/** POST /v1/organizations/{org_id}/users — super-admin-only user provisioning (no permission
 * gate: every admin_users row is already a super-admin, ADR-029). */
export function createUserForOrg(
  orgId: string,
  input: CreateUserInput,
  accessToken?: string | null,
): Promise<UserDirectoryEntry> {
  return request<UserDirectoryEntry>(`/organizations/${orgId}/users`, {
    method: 'POST',
    body: input,
    accessToken,
  });
}
