import { createContext, useContext } from 'react';

// ── What /auth/me actually returns ───────────────────────────────────────────

export interface BranchAccess {
  branch_id: number | null;
  branch_name: string | null;
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  role_code: string;
  role_name: string;
}

export interface UserInfoResponse {
  user_id: number;
  username: string;
  display_name: string;
  company_id: number;
  company_name: string;
  branches: BranchAccess[];
  /** Distinct permission codes across all active role assignments. */
  active_permissions: string[];
}

// ── Frontend user model ───────────────────────────────────────────────────────

export interface UserProfile {
  user_id: number;
  username: string;
  display_name: string;
  company_id: number;
  company_name: string;
  /**
   * Coarse scope derived from branch rows.  Do NOT use this alone to detect
   * driver/ODA users — a Driver+SpecificBranch assignment yields 'SpecificBranch'
   * here.  Use isDriverUser() from permissions.ts instead, which inspects the
   * raw `branches` array.
   */
  scope_type: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branch_ids: number[];
  /**
   * Derived from active_permissions (new path) or legacy PAYROLL_ADMIN
   * role_code check (old path).  True when the user can access Settings admin.
   */
  has_setup_manage: boolean;
  /** All distinct permission codes from the user's active role assignments. */
  active_permissions: string[];
  /** Display name of the primary role (first AllCompanyBranches branch, or first branch). */
  primary_role_name: string | null;
  /**
   * Raw branch/role rows from /auth/me, preserved verbatim.
   * Used by isDriverUser() to detect DRIVER role_code or ODA scope in any
   * assignment — including mixed and Driver+SpecificBranch cases that
   * scope_type alone cannot distinguish.
   */
  branches: readonly BranchAccess[];
}

// Derive the flat UserProfile from the API's UserInfoResponse
export function toUserProfile(info: UserInfoResponse): UserProfile {
  const hasAllBranches = info.branches.some(
    (b) => b.scope === 'AllCompanyBranches'
  );
  const isDriverOnly =
    info.branches.length > 0 &&
    info.branches.every((b) => b.scope === 'OwnDriverDataOnly');

  const perms: string[] = info.active_permissions ?? [];

  // has_setup_manage:
  //   1. Check new-path permission codes (settings.manage or setup.manage)
  //   2. Fall back to legacy role_code check so existing users work before
  //      their CompanyRoleID is populated in UserBranchRoles.
  const hasSetupManage =
    perms.includes('settings.manage') ||
    perms.includes('setup.manage') ||
    info.branches.some(
      (b) => b.scope === 'AllCompanyBranches' && b.role_code === 'PAYROLL_ADMIN'
    );

  return {
    user_id:            info.user_id,
    username:           info.username,
    display_name:       info.display_name,
    company_id:         info.company_id,
    company_name:       info.company_name,
    scope_type:         hasAllBranches
      ? 'AllCompanyBranches'
      : isDriverOnly
        ? 'OwnDriverDataOnly'
        : 'SpecificBranch',
    branch_ids:         info.branches
      .filter((b) => b.branch_id !== null)
      .map((b) => b.branch_id as number),
    has_setup_manage:   hasSetupManage,
    active_permissions: perms,
    primary_role_name:  (
      info.branches.find((b) => b.scope === 'AllCompanyBranches')?.role_name ??
      info.branches[0]?.role_name ??
      null
    ),
    branches:           info.branches,
  };
}

// ── Auth context ──────────────────────────────────────────────────────────────

export interface AuthState {
  user: UserProfile | null;
  isLoading: boolean;
  setUser: (user: UserProfile | null) => void;
  setLoading: (loading: boolean) => void;
  logout: () => void;
  /** Returns true when the user holds this permission code. */
  hasPermission: (code: string) => boolean;
  /** Returns true when the user holds at least one of the given codes. */
  hasAnyPermission: (codes: string[]) => boolean;
  /** Returns true when the user holds ALL of the given codes. */
  hasAllPermissions: (codes: string[]) => boolean;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
