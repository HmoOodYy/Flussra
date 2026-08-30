/**
 * Typed wrappers for payroll API calls.
 * Follows the same pattern as existing apiClient usage in the codebase.
 */
import apiClient from './apiClient';
import type {
  DayGridResponse,
  DayGridSaveRequest,
  PeriodPayLine,
  AddPeriodPayLineRequest,
  PeriodSummary,
  NextPeriodDates,
  EligibleDriver,
  EligibleDriversResponse,
  DriversOffResponse,
  FinalizationPreviewResponse,
  FinalLineSummary,
  DraftLineSummary,
  DriverPeriodSummary,
} from '../types/payroll';

// ---------------------------------------------------------------------------
// Next period dates — from branch Payroll Setup (source of truth for creation)
// ---------------------------------------------------------------------------

export async function getNextPeriodDates(branchId: number): Promise<NextPeriodDates> {
  const resp = await apiClient.get<NextPeriodDates>('/payroll/periods/next-period-dates', {
    params: { branch_id: String(branchId) },
  });
  return resp.data;
}

export type { NextPeriodDates };

// ---------------------------------------------------------------------------
// Period list (used by Review page)
// ---------------------------------------------------------------------------

export async function getPeriods(
  status?: string,
  branchId?: number,
): Promise<PeriodSummary[]> {
  const params: Record<string, string> = {};
  if (status) params.status = status;
  if (branchId != null) params.branch_id = String(branchId);
  const resp = await apiClient.get<PeriodSummary[]>('/payroll/periods', { params });
  return resp.data;
}

// ---------------------------------------------------------------------------
// Draft lines (used by Review detail dialog)
// ---------------------------------------------------------------------------

export async function getDraftLines(
  periodId: number,
  opts?: { status?: string },
): Promise<DraftLineSummary[]> {
  const params: Record<string, string> = {};
  if (opts?.status) params.status = opts.status;
  const resp = await apiClient.get<DraftLineSummary[]>(
    `/payroll/periods/${periodId}/lines`,
    { params },
  );
  return resp.data;
}

export async function getDriverPeriodSummary(
  periodId: number,
): Promise<DriverPeriodSummary[]> {
  const resp = await apiClient.get<DriverPeriodSummary[]>(
    `/payroll/periods/${periodId}/lines/summary`,
  );
  return resp.data;
}

export async function getDayGrid(
  periodId: number,
  workDate?: string,
): Promise<DayGridResponse> {
  const params: Record<string, string> = {};
  if (workDate) params.work_date = workDate;
  const resp = await apiClient.get<DayGridResponse>(
    `/payroll/periods/${periodId}/day-grid`,
    { params },
  );
  return resp.data;
}

export async function saveDayGrid(
  periodId: number,
  req: DayGridSaveRequest,
): Promise<DayGridResponse> {
  const resp = await apiClient.post<DayGridResponse>(
    `/payroll/periods/${periodId}/day-grid`,
    req,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// Current Payroll lifecycle actions
// ---------------------------------------------------------------------------

export async function submitPeriod(
  periodId: number,
): Promise<PeriodSummary> {
  // This is the backend's canonical Open submit route. Its CP-4D service path
  // captures the immutable snapshot and creates the Pending review item.
  const resp = await apiClient.patch<PeriodSummary>(
    `/payroll/periods/${periodId}/status`,
    { status: 'InReview' },
  );
  return resp.data;
}

export async function resubmitPeriod(periodId: number): Promise<PeriodSummary> {
  const resp = await apiClient.post<PeriodSummary>(
    `/payroll/periods/${periodId}/resubmissions`,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// CP-2 — Period pay lines (Bonus panel)
// ---------------------------------------------------------------------------

export async function getPeriodPayLines(
  periodId: number,
  lineType?: string,
): Promise<PeriodPayLine[]> {
  const params: Record<string, string> = {};
  if (lineType) params.line_type = lineType;
  const resp = await apiClient.get<PeriodPayLine[]>(
    `/payroll/periods/${periodId}/period-pay`,
    { params },
  );
  return resp.data;
}

export async function addPeriodPayLine(
  periodId: number,
  req: AddPeriodPayLineRequest,
): Promise<PeriodPayLine> {
  const resp = await apiClient.post<PeriodPayLine>(
    `/payroll/periods/${periodId}/period-pay`,
    req,
  );
  return resp.data;
}

export async function voidPeriodPayLine(
  periodId: number,
  lineId: number,
): Promise<PeriodPayLine> {
  const resp = await apiClient.delete<PeriodPayLine>(
    `/payroll/periods/${periodId}/period-pay/${lineId}`,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// CP-2 P1 #1 — Period-eligible drivers (stable Bonus dropdown)
// ---------------------------------------------------------------------------

export async function getEligibleDrivers(
  periodId: number,
): Promise<EligibleDriver[]> {
  const resp = await apiClient.get<EligibleDriversResponse>(
    `/payroll/periods/${periodId}/eligible-drivers`,
  );
  return resp.data.drivers;
}

// Re-export EligibleDriver so callers can import from payrollApi directly.
export type { EligibleDriver };

// ---------------------------------------------------------------------------
// CP-2.5 — Period-level Drivers Off
// ---------------------------------------------------------------------------

export async function getDriversOff(periodId: number): Promise<DriversOffResponse> {
  const resp = await apiClient.get<DriversOffResponse>(
    `/payroll/periods/${periodId}/drivers-off`,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// CP-3A/CP-3B — Finalization Preview + Finalize
// ---------------------------------------------------------------------------

export async function getFinalizationPreview(
  periodId: number,
): Promise<FinalizationPreviewResponse> {
  const resp = await apiClient.get<FinalizationPreviewResponse>(
    `/payroll/periods/${periodId}/finalization-preview`,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// CP-4 — Ledger: final lines
// ---------------------------------------------------------------------------

export async function getFinalLines(
  periodId: number,
  driverId?: number,
): Promise<FinalLineSummary[]> {
  const params: Record<string, string> = {};
  if (driverId != null) params.driver_id = String(driverId);
  const resp = await apiClient.get<FinalLineSummary[]>(
    `/payroll/periods/${periodId}/final-lines`,
    { params },
  );
  return resp.data;
}

export async function finalizePeriod(periodId: number): Promise<PeriodSummary> {
  const resp = await apiClient.post<PeriodSummary>(
    `/payroll/periods/${periodId}/finalize`,
  );
  return resp.data;
}
