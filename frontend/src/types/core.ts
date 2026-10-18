/**
 * Matches backend core DriverSummary — the permanent Driver plus the Branch of its
 * current (else pending, else latest) assignment. Payroll and Pay Rates address the
 * assignment (`driver_assignment_id`); the Driver identity is `driver_id`/`driver_number`.
 */
export interface DriverSummary {
  driver_id: number;
  employee_id: number;
  driver_assignment_id: number | null;
  branch_id: number;
  branch_name: string;
  full_name: string;
  preferred_name: string | null;
  employee_key: string | null;
  driver_number: string | null;
  cdl_number: string | null;
  driver_status: string;
  employment_status: string;
}

/** Matches backend PersonSummary — response of GET /core/people */
export interface PersonSummary {
  employee_id: number;
  branch_id: number;
  branch_name: string;
  employee_key: string | null;
  full_name: string;
  preferred_name: string | null;
  driver_state: 'current' | 'pending' | 'none';
  employment_status: string;
  email: string | null;
  primary_phone: string | null;
  hire_date: string | null;
  driver_id: number | null;
  driver_assignment_id: number | null;
  driver_number: string | null;
  driver_status: string | null;
  cdl_number: string | null;
}

export interface Branch {
  branch_id: number;
  branch_code: string;
  branch_name: string;
  status: string;
  is_default: boolean;
  city: string | null;
  state_province: string | null;
  country: string | null;
  time_zone_id: string;
  business_date: string;
}

/** Matches backend core DriverAssignmentSummary — one home-Branch tenure of a Driver. */
export interface DriverAssignmentInfo {
  driver_assignment_id: number;
  driver_id: number;
  branch_id: number;
  branch_name: string;
  effective_from_at: string;
  effective_to_at: string | null;
  end_reason: string | null;
  state: 'current' | 'pending' | 'ended';
}
