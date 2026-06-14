export interface DriverSummary {
  driver_id: number;
  employee_id: number;
  branch_id: number;
  branch_name: string;
  full_name: string;
  preferred_name: string | null;
  employee_key: string | null;
  driver_code: string | null;
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
  employee_type: string;
  employment_status: string;
  email: string | null;
  primary_phone: string | null;
  hire_date: string | null;
  driver_id: number | null;
  driver_code: string | null;
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
}
