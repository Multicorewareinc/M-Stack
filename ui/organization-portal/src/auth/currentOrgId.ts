/**
 * The authenticated organization id, mirrored here so the plain (non-React) API transport
 * layer (client.ts) can attach it as the real `X-Organization-Id` header the backend requires
 * (see organization-control-plane's require_org_context) — a fetch/request module can't call
 * useAuth() itself. Written by MockAuthProvider whenever organizationContext.id changes; read
 * by client.ts on every request. Unlike mocks/mockOrgContext.ts (MSW-only, thrown away with the
 * mock layer), this is the real mechanism and will still be needed once a real auth module lands.
 */
let currentOrgId: string | null = null;

export function setCurrentOrgId(id: string): void {
  currentOrgId = id;
}

export function getCurrentOrgId(): string | null {
  return currentOrgId;
}
