import { AccessDeniedPage } from '../pages/AccessDeniedPage';

interface Props {
  /** Pre-computed boolean: true = allow, false = show Access Denied. */
  allowed: boolean;
  children: React.ReactNode;
}

/**
 * Route-level permission guard.
 *
 * Usage in App.tsx:
 *   <PermissionGate allowed={canViewCurrentPayroll(user)}>
 *     <PeriodsListPage />
 *   </PermissionGate>
 *
 * Shows AccessDeniedPage for direct URL navigation when the user
 * lacks the required permission.  Backend 403s remain the real guard.
 */
export function PermissionGate({ allowed, children }: Props) {
  if (!allowed) return <AccessDeniedPage />;
  return <>{children}</>;
}
