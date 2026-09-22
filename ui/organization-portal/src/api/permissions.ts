import { request } from './client';
import type { Permission } from './types';

// TODO: confirm — Org CP has no GET /v1/permissions catalog endpoint yet (AD-08).
// This is read-only from the Organization Portal (§70-71); never mutated here.
export function listPermissions(accessToken?: string | null): Promise<Permission[]> {
  return request<Permission[]>('/permissions', { accessToken });
}
