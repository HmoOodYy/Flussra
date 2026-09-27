/**
 * Branch Assignments tab for the company-owned Payroll Policies page
 * (Phase 6 Unit A). Extracted from PayrollSetupsPage.tsx.
 *
 * Data comes from a single GET /payroll-setup/branch-summaries call (passed
 * down from the shell, shared with the policy detail's "Assigned branches"
 * section) — no per-branch history fetch for the table itself. Contains the
 * compact table, AssignPolicyModal, and ManageBranchDrawer.
 */
import { useState } from 'react';
import styles from './PayrollSetupsPage.module.css';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { describeReadiness } from '../../../lib/payrollSetupReadiness';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import {
  BRANCH_POLICY_ACTION_LABEL,
  branchPolicyActionKind,
  currentPolicyLabel,
  scheduledChangeLabel,
} from './branchPolicyView';
import { AssignPolicyModal } from './AssignPolicyModal';
import { ManageBranchDrawer } from './ManageBranchDrawer';
import type { BranchPolicySummaryResponse, SetupResponse } from '../../../types/payrollSetup';

type BranchAssignmentsTabProps = {
  summaries: readonly BranchPolicySummaryResponse[];
  summariesLoading: boolean;
  summariesError: ApiErrorInfo | null;
  activeSetups: readonly SetupResponse[];
  canAssign: boolean;
  mutating: boolean;
  setMutating: (value: boolean) => void;
  onReload: () => void;
  showToast: (message: string) => void;
};

export function BranchAssignmentsTab({
  summaries,
  summariesLoading,
  summariesError,
  activeSetups,
  canAssign,
  mutating,
  setMutating,
  onReload,
  showToast,
}: BranchAssignmentsTabProps) {
  const [assignTargetBranchId, setAssignTargetBranchId] = useState<number | null>(null);
  const [manageTargetBranchId, setManageTargetBranchId] = useState<number | null>(null);

  const assignTarget = summaries.find((s) => s.branch_id === assignTargetBranchId) ?? null;
  const manageTarget = summaries.find((s) => s.branch_id === manageTargetBranchId) ?? null;

  function handleAction(summary: BranchPolicySummaryResponse) {
    if (branchPolicyActionKind(summary) === 'assign') {
      setAssignTargetBranchId(summary.branch_id);
    } else {
      setManageTargetBranchId(summary.branch_id);
    }
  }

  return (
    <>
      <div className={styles.tableWrap}>
        {summariesError ? (
          <div className={styles.paddedNotice}>
            <PayrollErrorNotice info={summariesError} />
          </div>
        ) : summariesLoading ? (
          <p className={`${styles.mutedText} ${styles.paddedNotice}`}>Loading…</p>
        ) : summaries.length === 0 ? (
          <p className={`${styles.mutedText} ${styles.paddedNotice}`}>No branches found.</p>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Branch</th>
                <th>Current policy</th>
                <th>Scheduled change</th>
                <th>Status</th>
                {canAssign && <th>Action</th>}
              </tr>
            </thead>
            <tbody>
              {summaries.map((s) => {
                const readiness = describeReadiness(s.readiness_reason);
                const actionKind = branchPolicyActionKind(s);
                return (
                  <tr key={s.branch_id}>
                    <td>
                      {s.branch_name}
                      {s.branch_code && ` (${s.branch_code})`}
                    </td>
                    <td>{currentPolicyLabel(s)}</td>
                    <td>{scheduledChangeLabel(s)}</td>
                    <td title={readiness.description ?? undefined}>{readiness.label}</td>
                    {canAssign && (
                      <td>
                        <button
                          className={styles.btnSecondary}
                          onClick={() => handleAction(s)}
                          disabled={mutating}
                        >
                          {BRANCH_POLICY_ACTION_LABEL[actionKind]}
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {canAssign && assignTarget && (
        <AssignPolicyModal
          branchId={assignTarget.branch_id}
          branchName={assignTarget.branch_name}
          activeSetups={activeSetups}
          mutating={mutating}
          setMutating={setMutating}
          onClose={() => setAssignTargetBranchId(null)}
          onAssigned={() => {
            setAssignTargetBranchId(null);
            onReload();
          }}
          showToast={showToast}
        />
      )}

      {canAssign && manageTarget && (
        <ManageBranchDrawer
          key={manageTarget.branch_id}
          branchId={manageTarget.branch_id}
          branchName={manageTarget.branch_name}
          summary={manageTarget}
          activeSetups={activeSetups}
          mutating={mutating}
          setMutating={setMutating}
          onClose={() => setManageTargetBranchId(null)}
          onChanged={onReload}
          showToast={showToast}
        />
      )}
    </>
  );
}
