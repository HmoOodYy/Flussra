// ─── Role Assignments ─────────────────────────────────────────────────────────

export interface RoleAssignment {
  assignment_id: number;
  user_id: number;
  role_id: number;
  role_code: string;
  role_name: string;
  scope_type: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branch_id: number | null;
  branch_name: string | null;
  is_active: boolean;
  granted_by_user_id: number | null;
  granted_at_utc: string;
  revoked_at_utc: string | null;
  notes: string | null;
}

// ─── Users ────────────────────────────────────────────────────────────────────

export interface UserAdmin {
  user_id: number;
  company_id: number;
  username: string;
  display_name: string;
  email: string | null;
  phone: string | null;
  is_active: boolean;
  can_login: boolean;
  must_change_password: boolean;
  last_login_at_utc: string | null;
  created_at_utc: string;
  updated_at_utc: string | null;
  // NOTE: includes BOTH active and revoked assignments — always filter by is_active before displaying
  role_assignments: RoleAssignment[];
  // ── Company-role (new path) ──
  company_role_assignment_id: number | null;
  company_role_id: number | null;
  company_role_code: string | null;
  company_role_name: string | null;
  company_role_scope: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly' | null;
  company_role_branch_id: number | null;
  company_role_branch_name: string | null;
  // ── Driver profile ──
  driver_id: number | null;
  // ── Permissions ──
  /** Permission codes granted by the current company role */
  role_permission_codes: string[];
  /** Extra ALLOW overrides granted directly to this user */
  extra_permission_codes: string[];
}

export interface UserCreate {
  username: string;
  display_name: string;
  password: string;
  email?: string | null;
  phone?: string | null;
  is_active?: boolean;
  can_login?: boolean;
  must_change_password?: boolean;
}

export interface UserUpdate {
  display_name?: string | null;
  email?: string | null;
  phone?: string | null;
  is_active?: boolean | null;
  can_login?: boolean | null;
  must_change_password?: boolean | null;
}

export interface UserPasswordReset {
  new_password: string;
  must_change_password?: boolean;
}

// ─── Role Assignment CRUD ─────────────────────────────────────────────────────

export interface RoleAssignmentCreate {
  role_id: number;
  scope_type: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branch_id?: number | null;
  notes?: string | null;
}

// ─── Roles & Permissions ──────────────────────────────────────────────────────

export interface Role {
  role_id: number;
  role_code: string;
  role_name: string;
  role_level: number;
  is_system_role: boolean;
  notes: string | null;
}

export interface Permission {
  permission_id: number;
  permission_code: string;
  permission_name: string;
  module_code: string;
  notes: string | null;
}

// ─── Company Roles (new company-scoped role system) ───────────────────────────

export interface CompanyRole {
  company_role_id: number;
  company_id: number;
  role_code: string;
  role_name: string;
  role_level: number;
  is_default: boolean;
  is_protected: boolean;
  is_custom: boolean;
  is_active: boolean;
  notes: string | null;
  created_at_utc: string;
  updated_at_utc: string | null;
  user_count: number;
}

export interface CompanyRoleCreate {
  role_name: string;
  notes?: string | null;
  // role_code is generated server-side (CR_XXXXXXXX) — do not send from UI
}

export interface CompanyRoleUpdate {
  role_name?: string | null;
  is_active?: boolean | null;
  notes?: string | null;
}

export interface CompanyRolePermissions {
  company_role_id: number;
  permission_codes: string[];
}

export interface CompanyRolePermissionsUpdate {
  permission_codes: string[];
}

// ─── Company Role Assignments (new path) ─────────────────────────────────────

export interface CompanyRoleAssignmentCreate {
  company_role_id: number;
  scope_type: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branch_id?: number | null;
  notes?: string | null;
}

export interface CompanyRoleAssignmentDetail {
  assignment_id: number;
  user_id: number;
  company_role_id: number;
  company_role_code: string;
  company_role_name: string;
  scope_type: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branch_id: number | null;
  branch_name: string | null;
  is_active: boolean;
  granted_by_user_id: number | null;
  granted_at_utc: string;
  revoked_at_utc: string | null;
  notes: string | null;
}

// ─── User Permission Overrides ────────────────────────────────────────────────

export interface UserPermissionOverridesUpdate {
  permission_codes: string[];
}

// ─── Ownership Transfer ───────────────────────────────────────────────────────

export interface OwnerTransferRequest {
  target_user_id: number;
  replacement_company_role_id?: number | null;
  confirmation: 'TRANSFER';
}

export interface OwnerTransferResult {
  new_owner_user_id: number;
  new_owner_display_name: string;
  previous_owner_user_id: number;
  previous_owner_display_name: string;
  previous_owner_new_role_id: number | null;
  previous_owner_new_role_name: string | null;
}

// ─── Company Role Users ───────────────────────────────────────────────────────

export interface CompanyRoleUser {
  assignment_id: number;
  user_id: number;
  username: string;
  display_name: string;
  email: string | null;
  scope_type: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branch_id: number | null;
  branch_name: string | null;
  is_active: boolean;
  granted_at_utc: string;
}
