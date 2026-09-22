/**
 * Mock-only, test-seam state (SP-13 tenant-isolation hardening): which org the mock backend is
 * currently "serving" for handlers that vary by tenant (only `GET /v1/roles` and the org summary
 * do today — this is intentionally thin, not a full multi-tenant mock rewrite). Written by
 * MockAuthProvider whenever `organizationContext.id` changes; read by mocks/handlers.ts.
 *
 * MSW's browser worker relays requests to handler code running in this same page bundle (not a
 * separate JS realm), so this plain module-level variable is visible to both sides — no need for
 * a header or any wire-level signal.
 */
let currentMockOrgId = 'org_001';

export function setCurrentMockOrgId(id: string): void {
  currentMockOrgId = id;
}

export function getCurrentMockOrgId(): string {
  return currentMockOrgId;
}
