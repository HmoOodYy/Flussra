import type { Dispatch, SetStateAction } from 'react';
import { Link } from 'react-router-dom';
import { PayrollBoundaryDateInput } from '../../../components/payroll/PayrollBoundaryDateInput';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import type { BranchAdmin } from '../../../types/settings';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import type { CompanyBranchesCapabilities } from './companyBranchesView';
import { showOnboardingCheckbox } from './companyBranchesView';
import type { BranchForm } from './branchForm';
import type { BranchOnboardingState } from './useBranchOnboarding';
import type { ConfirmKind } from './companyBranchesPresentation';
import { AlertIcon, CloseIcon, SpinnerIcon, StarIcon } from './CompanyBranchesIcons';
import { BranchFormFields } from './BranchFormFields';
import styles from './CompanyBranchesPage.module.css';

export function BranchEditorDialog({
  modal, editingBranch, closeModal, savingBranch, caps, settingDefault,
  setConfirmKind, bForm, setBForm, onboarding, bSaveErr, bCreateErr, canCreateNow,
}: {
  modal: 'create' | 'edit' | null; editingBranch: BranchAdmin | null;
  closeModal: () => void; savingBranch: boolean; caps: CompanyBranchesCapabilities;
  settingDefault: boolean; setConfirmKind: Dispatch<SetStateAction<ConfirmKind | null>>;
  bForm: BranchForm; setBForm: Dispatch<SetStateAction<BranchForm>>;
  onboarding: BranchOnboardingState; bSaveErr: string;
  bCreateErr: ApiErrorInfo | null; canCreateNow: boolean;
}) {
  const {
    firstPayrollStart, setFirstPayrollStart, setUpPayrollNow, setSetUpPayrollNow,
    onboardingDefaultSetup, onboardingDefaultLoading, onboardingDefaultError,
    onboardingBoundary, hasCompanyDefault,
  } = onboarding;
  return (
    <>
      {/* ── Branch modal ── */}
      {modal && (
        <div className={styles.modalOverlay} onClick={(e) => { if (e.target === e.currentTarget) closeModal(); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {modal === 'create' ? 'Add Branch' : `Edit Branch — ${editingBranch?.branch_name}`}
              </h2>
              <button className={styles.modalCloseBtn} onClick={closeModal}><CloseIcon /></button>
            </div>
            <div className={styles.modalBody}>
              {/* Default branch status — edit mode only */}
              {modal === 'edit' && editingBranch && (
                <div className={styles.defaultSection}>
                  {editingBranch.is_default ? (
                    <div className={styles.defaultInfoRow}>
                      <span style={{ color: '#2563eb', display: 'flex', flexShrink: 0 }}><StarIcon /></span>
                      <div className={styles.defaultInfoText}>
                        <span className={styles.defaultInfoLabel}>Current Default Branch</span>
                        <span className={styles.defaultInfoDesc}>
                          To change the default, open another branch and set it as default.
                        </span>
                      </div>
                    </div>
                  ) : modal === 'edit' && caps.canAdmin && editingBranch.status === 'Active' && (
                    <button
                      className={styles.btnSetDefault}
                      onClick={() => setConfirmKind('set-default')}
                      disabled={savingBranch || settingDefault}
                    >
                      {settingDefault ? <><SpinnerIcon /> Setting…</> : <><StarIcon /> Set as Default Branch</>}
                    </button>
                  )}
                </div>
              )}

              <BranchFormFields
                form={bForm}
                onChange={(p) => setBForm((f) => ({ ...f, ...p }))}
                disabled={savingBranch}
                isCreate={modal === 'create'}
                canSetDefault={caps.canSetDefaultOnCreate}
                styles={styles}
              />

              {modal === 'create' && showOnboardingCheckbox(caps) && (
                <div className={styles.onboardingSection}>
                  <label className={styles.onboardingCheckboxRow}>
                    <input
                      type="checkbox"
                      checked={setUpPayrollNow}
                      onChange={(e) => {
                        setSetUpPayrollNow(e.target.checked);
                        setFirstPayrollStart('');
                      }}
                      disabled={savingBranch}
                    />
                    <span>Set up payroll for this branch now</span>
                  </label>

                  {setUpPayrollNow && (
                    onboardingDefaultError ? (
                      <PayrollErrorNotice info={onboardingDefaultError} />
                    ) : onboardingDefaultLoading ? (
                      <p className={styles.fieldHelp}>Loading…</p>
                    ) : hasCompanyDefault ? (
                      <>
                        <div className={styles.onboardingDefaultInfo}>
                          <span className={styles.onboardingDefaultInfoLabel}>Payroll policy</span>
                          <span>{`Company default: ${onboardingDefaultSetup!.setup_name}`}</span>
                        </div>
                        <p className={styles.fieldHelp}>
                          This branch will use the company&apos;s default payroll policy.
                        </p>
                        <PayrollBoundaryDateInput
                          id="branch-onboarding-date"
                          label="First payroll period starts on"
                          value={firstPayrollStart}
                          onChange={setFirstPayrollStart}
                          choices={onboardingBoundary.choices}
                          loading={onboardingBoundary.loading}
                          error={onboardingBoundary.error}
                          disabled={savingBranch}
                          context="onboarding"
                        />
                      </>
                    ) : (
                      <div className={styles.onboardingMissingDefault}>
                        <p>
                          No company default payroll policy is set, so payroll can&apos;t be set up for this branch
                          yet.
                        </p>
                        {caps.canViewSetups ? (
                          <Link to="/settings/payroll">Choose a default payroll policy</Link>
                        ) : (
                          <span>Ask an administrator to choose a company default payroll policy.</span>
                        )}
                      </div>
                    )
                  )}
                </div>
              )}
              {modal === 'create' && !caps.canOnboard && (
                <p className={`${styles.fieldHelp} ${styles.onboardingUnavailableNote}`}>
                  Setting up payroll for a new branch requires the payroll policy assign permission. The branch can
                  still be created without it.
                </p>
              )}

              {bSaveErr && <div className={styles.errorAlert} style={{ marginTop: '0.75rem' }}><AlertIcon /> {bSaveErr}</div>}
              {bCreateErr && <PayrollErrorNotice info={bCreateErr} />}
            </div>
            <div className={styles.modalFooter}>
              <button
                className={styles.btnPrimary}
                onClick={() => setConfirmKind('save-branch')}
                disabled={savingBranch || (modal === 'create' && !canCreateNow)}
              >
                {savingBranch ? <><SpinnerIcon /> Saving…</> : modal === 'create' ? 'Create Branch' : 'Save Changes'}
              </button>
              <button className={styles.btnSecondary} onClick={closeModal} disabled={savingBranch}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
