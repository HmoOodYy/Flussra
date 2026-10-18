import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { usageStateLabel } from './policyDetailView';
import type { PolicyScheduleFields } from './policyDetailView';
import type { ActionError, ConfirmKind } from './policyDetailTypes';
import type { PolicyUsageState } from './policyMasterView';
import type { SetupResponse } from '../../../types/payrollSetup';
import styles from './PayrollSetupsPage.module.css';

type Props = {
  selectedPolicy: SetupResponse;
  usageState: PolicyUsageState;
  defaultSetupId: number | null;
  openEditModal: () => void;
  openConfirm: (kind: ConfirmKind) => void;
  mutating: boolean;
  defaultPrerequisiteMet: boolean;
  defaultBlockedByNoPublishedVersion: boolean;
  canAssign: boolean;
  canManage: boolean;
  actionError: ActionError | null;
  selectedPolicyId: number | null;
  versionsError: ApiErrorInfo | null;
  versionsLoading: boolean;
  scheduleFields: PolicyScheduleFields | null;
  referenceBranchName: string | null;
};

export function PolicyDetailHeader(p: Props) {
  const {
    selectedPolicy,
    usageState,
    defaultSetupId,
    openEditModal,
    openConfirm,
    mutating,
    defaultPrerequisiteMet,
    defaultBlockedByNoPublishedVersion,
    canAssign,
    canManage,
    actionError,
    selectedPolicyId,
    versionsError,
    versionsLoading,
    scheduleFields,
    referenceBranchName,
  } = p;
  return (
        <div className={styles.policyHeader}>
          <div className={styles.policyHeaderTop}>
            <div className={styles.policyHeaderMain}>
              <div className={styles.policyTitleRow}>
                <h2 className={styles.policyTitle}>{selectedPolicy.setup_name}</h2>
                <span
                  className={
                    usageState === 'Active'
                      ? `${styles.badge} ${styles.badgeGreen}`
                      : usageState === 'Archived'
                        ? `${styles.badge} ${styles.badgeMuted}`
                        : `${styles.badge} ${styles.badgeGray}`
                  }
                >
                  {usageStateLabel(usageState)}
                </span>
                {selectedPolicy.setup_id === defaultSetupId && (
                  <span className={`${styles.badge} ${styles.badgeBlue}`}>Default for new branches</span>
                )}
              </div>
              {selectedPolicy.description && <p className={styles.policyDescription}>{selectedPolicy.description}</p>}
            </div>
            <div className={styles.policyHeaderActions}>
              <div className={styles.policyActionRow}>
                {canAssign && selectedPolicy.status === 'Active' && selectedPolicy.setup_id !== defaultSetupId && (
                  <button
                    className={styles.btnSecondary}
                    onClick={() => openConfirm('set-default')}
                    disabled={mutating || !defaultPrerequisiteMet}
                    aria-describedby={defaultBlockedByNoPublishedVersion ? 'policy-default-hint' : undefined}
                  >
                    Set as default
                  </button>
                )}
                {selectedPolicy.setup_id === defaultSetupId && canAssign && (
                  <button className={styles.btnDanger} onClick={() => openConfirm('clear-default')} disabled={mutating}>
                    Clear company default
                  </button>
                )}
                {canManage && selectedPolicy.status === 'Active' && (
                  <button className={styles.btnSecondary} onClick={openEditModal} disabled={mutating}>
                    Edit details
                  </button>
                )}
              </div>
              {defaultBlockedByNoPublishedVersion &&
                canAssign &&
                selectedPolicy.status === 'Active' &&
                selectedPolicy.setup_id !== defaultSetupId && (
                  <p id="policy-default-hint" className={styles.policyActionHint}>
                    Publish a payroll schedule before setting this policy as the default.
                  </p>
                )}
            </div>
          </div>
          {actionError?.kind === 'default' &&
            (actionError.setupId === null || actionError.setupId === selectedPolicyId) && (
              <div className={styles.policyHeaderNotice}>
                <PayrollErrorNotice info={actionError.info} />
              </div>
            )}

          {/* ── Payroll schedule: the currently effective published Version ── */}
          <p className={styles.policyScheduleContext}>
            {referenceBranchName
              ? `Current version using ${referenceBranchName}'s business date`
              : 'Select a Branch above to identify the current version.'}
          </p>
          <section className={styles.policyDetailsCard} aria-label="Payroll schedule">
          <div className={styles.policyDetailsHead}>
              <div className={styles.policyDetailsHeading}>
                <span className={styles.policyDetailsIcon} aria-hidden="true">▤</span>
                <div>
                  <h3 className={styles.policyDetailsTitle}>Payroll schedule</h3>
                  <p className={styles.policyDetailsSubtitle}>The published schedule currently in effect for this policy.</p>
                </div>
              </div>
            </div>
          {versionsError ? (
            <div className={styles.policyDetailsBody}>
              <PayrollErrorNotice info={versionsError} />
            </div>
          ) : versionsLoading ? (
            <div className={styles.policyDetailsBody}>
              <p className={styles.mutedText}>Loading…</p>
            </div>
          ) : scheduleFields === null ? (
            <div className={styles.scheduleEmpty}>
              {referenceBranchName ? <>
                <p className={styles.scheduleEmptyTitle}>No payroll schedule is active for this date</p>
                <p className={styles.scheduleEmptyText}>Choose a Branch with a current version, or publish a schedule.</p>
              </> : <>
                <p className={styles.scheduleEmptyTitle}>Select a Branch to identify the current version</p>
                <p className={styles.scheduleEmptyText}>Published schedules are shared configuration; the Branch date provides current-version context.</p>
              </>}
            </div>
          ) : (
            <dl className={styles.policyDetailsGrid}>
              <div className={styles.policyDetailsItem}>
                <dt>Payroll frequency</dt>
                <dd>{scheduleFields.frequency}</dd>
              </div>
              <div className={styles.policyDetailsItem}>
                <dt>First payroll period starts on</dt>
                <dd>{scheduleFields.firstPeriodStart}</dd>
              </div>
              {scheduleFields.interval !== null && (
                <div className={styles.policyDetailsItem}>
                  <dt>Custom interval</dt>
                  <dd>{scheduleFields.interval}</dd>
                </div>
              )}
              <div className={styles.policyDetailsItem}>
                <dt>Regular days off</dt>
                <dd>{scheduleFields.daysOff}</dd>
              </div>
            </dl>
          )}
          </section>
        </div>
  );
}
