import type * as React from 'react';
import { usePermission } from '../hooks/usePermission';
import { AccessDenied } from '../app/AccessDenied';

export interface RequirePermissionProps {
  /** A single permission, or an array matched with OR semantics (any one grants access). */
  permission: string | string[];
  children: React.ReactNode;
}

/**
 * Route-level counterpart to <Can> (§73-76) — where <Can> hides a button, this hides an entire
 * page behind the same Access Denied screen the Shell already shows for a backend 403
 * (app/AccessDenied.tsx), so a user without the permission sees one consistent "you can't be
 * here" experience whether they clicked a hidden-then-somehow-reached link, typed the URL
 * directly, or hit a real backend 403. Still UX only — the backend is the real boundary
 * (usePermission's own doc comment); this just avoids rendering a page (and firing its data
 * fetches) for a section already known to be off-limits.
 */
export function RequirePermission({ permission, children }: RequirePermissionProps) {
  const allowed = usePermission(permission);
  return allowed ? <>{children}</> : <AccessDenied />;
}
