import { request } from './client';
import type { Plan } from './types';

export function listPlans(accessToken?: string | null): Promise<Plan[]> {
  return request<Plan[]>('/plans', { accessToken });
}

export interface PlanInput {
  name: string;
  tpm?: number;
  rpm?: number;
  quota_monthly_tokens?: number;
}

export function createPlan(input: PlanInput, accessToken?: string | null): Promise<Plan> {
  return request<Plan>('/plans', { method: 'POST', body: input, accessToken });
}

export function updatePlan(
  id: string,
  input: Partial<PlanInput>,
  accessToken?: string | null,
): Promise<Plan> {
  return request<Plan>(`/plans/${id}`, { method: 'PATCH', body: input, accessToken });
}

/** Soft-deactivate only — there is no delete export; a plan is never hard-deleted (§47). */
export function deactivatePlan(id: string, accessToken?: string | null): Promise<Plan> {
  return request<Plan>(`/plans/${id}`, { method: 'PATCH', body: { is_active: false }, accessToken });
}

export function activatePlan(id: string, accessToken?: string | null): Promise<Plan> {
  return request<Plan>(`/plans/${id}`, { method: 'PATCH', body: { is_active: true }, accessToken });
}
