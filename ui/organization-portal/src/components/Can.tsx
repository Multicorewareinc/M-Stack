import type * as React from 'react';
import { usePermission } from '../hooks/usePermission';

export interface CanProps {
  permission: string;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

/** Renders children only when the mock identity carries `permission`. UX only — see usePermission. */
export function Can({ permission, children, fallback = null }: CanProps) {
  const allowed = usePermission(permission);
  return <>{allowed ? children : fallback}</>;
}
