/**
 * Typed wrappers for payroll API calls.
 * Follows the same pattern as existing apiClient usage in the codebase.
 */
import apiClient from './apiClient';
import type {
  DayGridResponse,
  DayGridSaveRequest,
  BonusEvent,
  BonusBatchCreate,
  BonusBatchResponse,
  BonusSummary,
  PeriodSummary,
  SelectedDayOffDriversResponse,
  FinalizationPreviewResponse,
  CalculationPreviewResponse,
  FinalLineSummary,
  CurrentWorkflow,
  CurrentPayrollHub,
  PeriodCandidateMode,
  PeriodCandidatePreview,
  PeriodCreationResult,
  CalculationReportResponse,
  CalculationReportView,
  FinalizedCalculationReportResponse,
  FinalizedPeriodListItem,
  FinalizedOverviewResponse,
  FinalizedReportView,
  FinalizedOffDriversResponse,
  FinalizedRatesUsedResponse,
  FinalizedAuditResponse,
} from '../types/payroll';

// ---------------------------------------------------------------------------
// Current workflow and candidate-based period creation
// ---------------------------------------------------------------------------

export async function getCurrentWorkflow(branchId?: number): Promise<CurrentWorkflow> {
  const params = branchId == null ? undefined : { branch_id: String(branchId) };
  const resp = await apiClient.get<CurrentWorkflow>('/payroll/current-workflow', { params });
  return resp.data;
}

export async function getCurrentPayrollHub(branchId?: number): Promise<CurrentPayrollHub> {
  const params = branchId == null ? undefined : { branch_id: String(branchId) };
  const resp = await apiClient.get<CurrentPayrollHub>('/payroll/current', { params });
  return resp.data;
}

export async function getPeriodCandidate(
  branchId: number,
  mode: PeriodCandidateMode,
): Promise<PeriodCandidatePreview> {
  const resp = await apiClient.get<PeriodCandidatePreview>(
    `/payroll/branches/${branchId}/period-candidates`,
    { params: { mode } },
  );
  return resp.data;
}

export async function createPeriodFromCandidate(
  branchId: number,
  candidateKey: string,
): Promise<PeriodCreationResult> {
  const resp = await apiClient.post<PeriodCreationResult>(
    `/payroll/branches/${branchId}/period-creations`,
    { candidate_key: candidateKey },
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
// CP-3A — Canonical BonusEvents
// ---------------------------------------------------------------------------

export async function getBonusSummary(periodId: number): Promise<BonusSummary> {
  const resp = await apiClient.get<BonusSummary>(
    `/payroll/periods/${periodId}/bonuses/summary`,
  );
  return resp.data;
}

export async function createBonusBatch(
  periodId: number,
  req: BonusBatchCreate,
): Promise<BonusBatchResponse> {
  const resp = await apiClient.post<BonusBatchResponse>(
    `/payroll/periods/${periodId}/bonuses/batch`,
    req,
  );
  return resp.data;
}

export async function voidBonusEvent(
  periodId: number,
  bonusEventId: number,
): Promise<BonusEvent> {
  const resp = await apiClient.delete<BonusEvent>(
    `/payroll/periods/${periodId}/bonuses/${bonusEventId}`,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// CP-2.5 — Period-level Drivers Off
// ---------------------------------------------------------------------------

export async function getSelectedDayOffDrivers(
  periodId: number,
  workDate: string,
): Promise<SelectedDayOffDriversResponse> {
  const resp = await apiClient.get<SelectedDayOffDriversResponse>(
    `/payroll/periods/${periodId}/off-drivers`,
    { params: { work_date: workDate } },
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// CP-4B — Open/Returned live calculation preview
// ---------------------------------------------------------------------------

export async function getCalculationPreview(
  periodId: number,
): Promise<CalculationPreviewResponse> {
  const resp = await apiClient.get<CalculationPreviewResponse>(
    `/payroll/periods/${periodId}/calculation-preview`,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// CP-5C — Current Payroll calculation reports
// ---------------------------------------------------------------------------

export async function getCalculationReport(
  periodId: number,
  view: CalculationReportView,
): Promise<CalculationReportResponse> {
  const resp = await apiClient.get<CalculationReportResponse>(
    `/payroll/periods/${periodId}/reports/${view}`,
  );
  return resp.data;
}

// ---------------------------------------------------------------------------
// P6A — Finalized Payroll Information Library
// ---------------------------------------------------------------------------

export async function getFinalizedPeriods(
  status?: 'Locked' | 'Archived',
  branchId?: number,
): Promise<FinalizedPeriodListItem[]> {
  const params: Record<string, string> = {};
  if (status) params.status = status;
  if (branchId != null) params.branch_id = String(branchId);
  const resp = await apiClient.get<FinalizedPeriodListItem[]>('/payroll/finalized', { params });
  return resp.data;
}

export async function getFinalizedOverview(
  periodId: number,
): Promise<FinalizedOverviewResponse> {
  const resp = await apiClient.get<FinalizedOverviewResponse>(
    `/payroll/finalized/${periodId}/overview`,
  );
  return resp.data;
}

export async function getFinalizedReport(
  periodId: number,
  view: FinalizedReportView,
): Promise<FinalizedCalculationReportResponse> {
  const resp = await apiClient.get<FinalizedCalculationReportResponse>(
    `/payroll/finalized/${periodId}/reports/${view}`,
  );
  return resp.data;
}

export async function getFinalizedOffDrivers(
  periodId: number,
): Promise<FinalizedOffDriversResponse> {
  const resp = await apiClient.get<FinalizedOffDriversResponse>(
    `/payroll/finalized/${periodId}/off-drivers`,
  );
  return resp.data;
}

export async function getFinalizedRatesUsed(
  periodId: number,
): Promise<FinalizedRatesUsedResponse> {
  const resp = await apiClient.get<FinalizedRatesUsedResponse>(
    `/payroll/finalized/${periodId}/rates-used`,
  );
  return resp.data;
}

export async function getFinalizedAudit(
  periodId: number,
): Promise<FinalizedAuditResponse> {
  const resp = await apiClient.get<FinalizedAuditResponse>(
    `/payroll/finalized/${periodId}/audit`,
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
