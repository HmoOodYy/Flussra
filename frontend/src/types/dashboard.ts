export interface LastFinalizedPeriod {
  period_id: number;
  period_name: string;
  branch_id: number;
  branch_name: string;
  start_date: string;
  end_date: string;
  locked_at: string | null;
}

export interface ApprovedPeriodItem {
  period_id: number;
  period_name: string;
  branch_id: number;
  branch_name: string;
  start_date: string;
  end_date: string;
}

export interface SetupWarning {
  code: string;
  severity: string;
  message: string;
  branch_id: number | null;
  branch_name: string | null;
  count: number | null;
}

export interface BranchPeriodSummary {
  branch_id: number;
  branch_name: string;
  draft_count: number;
  open_count: number;
  in_review_count: number;
  approved_count: number;
  locked_count: number;
  pending_review_count: number;
  edit_requested_count: number;
  active_driver_count: number;
  needs_manager_review_lines: number;
}

export interface DashboardResponse {
  generated_at: string;
  scope: string;

  /** Which sections are populated — frontend uses this to decide what to render. */
  sections_available: string[];

  // Payroll Ops
  periods_draft: number;
  periods_open: number;
  periods_in_review: number;
  periods_approved: number;
  periods_locked: number;

  // Ready to Finalize (payroll.finalize users only)
  approved_periods: ApprovedPeriodItem[];

  // Review Queue
  review_pending: number;
  review_edit_requested: number;

  // People & Drivers
  active_drivers: number;
  pending_transfers: number | null;

  // Rates Health
  pending_rates: number | null;

  last_finalized_period: LastFinalizedPeriod | null;
  setup_warnings: SetupWarning[];
  branch_summaries: BranchPeriodSummary[];
}
