import { request } from './client';
import type { PlatformSummary } from './types';

// TODO: confirm — mirrors SP-02's mocked /platform-summary endpoint, which
// does not exist server-side yet (AD-08).
export function getPlatformSummary(accessToken?: string | null): Promise<PlatformSummary> {
  return request<PlatformSummary>('/platform-summary', { accessToken });
}
