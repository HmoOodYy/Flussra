// Workforce contracts — mirrors backend/app/workforce/schemas.py.
// Employee → optional permanent Driver → time-bounded DriverAssignments.

export type EmploymentStatus = 'Active' | 'Inactive' | 'Terminated';
export type DriverState = 'current' | 'pending' | 'none';
export type DriverWorkStatus = 'Active' | 'Inactive' | 'OnLeave';

/** The permanent Driver identity. */
export interface WorkforceDriver {
  driver_id: number;
  driver_number: string | null;
  cdl_number: string | null;
  driver_status: DriverWorkStatus | string;
}

/** One continuous home-Branch tenure of the permanent Driver. */
export interface DriverAssignmentSummary {
  driver_assignment_id: number;
  driver_id: number;
  branch_id: number;
  branch_name: string;
  effective_from_at: string;
  effective_to_at: string | null;
  end_reason: string | null;
  transferred_from_assignment_id: number | null;
  transferred_to_assignment_id: number | null;
}

export interface EmployeeSummary {
  employee_id: number;
  company_id: number;
  branch_id: number;
  branch_name: string;
  employee_key: string | null;
  full_name: string;
  preferred_name: string | null;
  email: string | null;
  primary_phone: string | null;
  hire_date: string | null;
  employment_status: EmploymentStatus | string;
  termination_date: string | null;
  driver_state: DriverState;
  driver: WorkforceDriver | null;
  current_or_pending_assignment: DriverAssignmentSummary | null;
}

/** The account linked to an Employee, as the Workforce read reports it. */
export interface LinkedUserSummary {
  user_id: number;
  username: string;
  display_name: string;
  is_active: boolean;
  can_login: boolean;
  is_staged: boolean;
}

export interface EmployeeDetail extends EmployeeSummary {
  driver_assignments: DriverAssignmentSummary[];
  linked_user: LinkedUserSummary | null;
}

export interface EmployeeTerminationResult extends EmployeeDetail {
  cancelled_transfer_request_ids: number[];
}

export interface DriverCreatePayload {
  driver_number?: string | null;
  cdl_number?: string | null;
  effective_from_at?: string | null;
}

export interface EmployeeCreatePayload {
  branch_id: number;
  employee_key?: string | null;
  full_name: string;
  preferred_name?: string | null;
  email?: string | null;
  primary_phone?: string | null;
  hire_date?: string | null;
  employment_status?: 'Active' | 'Inactive';
  /** Present means "Add Driver": the Employee, permanent Driver and first assignment are created together. */
  driver?: DriverCreatePayload | null;
}

export interface EmployeeUpdatePayload {
  branch_id?: number | null;
  full_name?: string;
  preferred_name?: string | null;
  email?: string | null;
  primary_phone?: string | null;
  hire_date?: string | null;
  employment_status?: EmploymentStatus;
  termination_date?: string | null;
}

export interface DriverUpdatePayload {
  driver_number?: string | null;
  cdl_number?: string | null;
  driver_status?: DriverWorkStatus;
}

export interface EmployeeTerminationPayload {
  termination_date: string;
  reason: string;
}

export interface EmployeeListParams {
  branch_id?: number;
  employment_status?: string;
  driver_state?: DriverState;
  q?: string;
}
