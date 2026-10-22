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

/** Matches backend core DriverAssignmentRosterRow — an assignment with the permanent Driver's identity. */
export interface DriverAssignmentRosterRow extends DriverAssignmentInfo {
  driver_number: string | null;
  driver_status: string;
  full_name: string;
  preferred_name: string | null;
}
