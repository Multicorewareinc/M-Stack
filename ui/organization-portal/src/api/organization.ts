import { request } from './client';
import type { OrgSummary } from './types';

// TODO: confirm — /organization/summary is mocked (AD-06, AD-08); in the real
// system Org CP resolves plan entitlement via an internal call to Admin CP
// (blueprint §55, §100).
export function getOrgSummary(accessToken?: string | null): Promise<OrgSummary> {
  return request<OrgSummary>('/organization/summary', { accessToken });
}
