import { request } from './client';
import type { Organization } from './types';

export interface CreateOrganizationInput {
  name: string;
  plan_id: string;
}

export function listOrganizations(search = '', accessToken?: string | null): Promise<Organization[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : '';
  return request<Organization[]>(`/organizations${qs}`, { accessToken });
}

export function getOrganization(id: string, accessToken?: string | null): Promise<Organization> {
  return request<Organization>(`/organizations/${id}`, { accessToken });
}

export function createOrganization(input: CreateOrganizationInput, accessToken?: string | null): Promise<Organization> {
  return request<Organization>('/organizations', { method: 'POST', body: input, accessToken });
}

export function getOrganizationUsers(id: string, accessToken?: string | null) {
  return request(`/organizations/${id}/users`, { accessToken });
}

export interface UpdateOrganizationInput {
  name?: string;
}

export function updateOrganization(
  id: string,
  input: UpdateOrganizationInput,
  accessToken?: string | null,
): Promise<Organization> {
  return request<Organization>(`/organizations/${id}`, { method: 'PATCH', body: input, accessToken });
}

export function changeOrganizationPlan(
  id: string,
  planId: string,
  accessToken?: string | null,
): Promise<Organization> {
  return request<Organization>(`/organizations/${id}`, {
    method: 'PATCH',
    body: { plan_id: planId },
    accessToken,
  });
}

export function suspendOrganization(id: string, accessToken?: string | null): Promise<Organization> {
  return request<Organization>(`/organizations/${id}`, {
    method: 'PATCH',
    body: { status: 'suspended' },
    accessToken,
  });
}

export function retryProvisioning(id: string, accessToken?: string | null): Promise<Organization> {
  return request<Organization>(`/organizations/${id}/retry`, { method: 'POST', accessToken });
}

export function deleteOrganization(id: string, accessToken?: string | null): Promise<void> {
  return request<void>(`/organizations/${id}`, { method: 'DELETE', accessToken });
}
