import { useState, useCallback, useRef } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuth } from '../../../store/authStore';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { ReadOnlyBanner } from '../../../components/ui';
import { formatIsoLong } from '../../../lib/isoDate';
import {
  companyBranchesCapabilities,
  accessNotice,
} from './companyBranchesView';
import { useBranchOnboarding } from './useBranchOnboarding';
import { useCompanyProfile } from './useCompanyProfile';
import { useBranchManagement } from './useBranchManagement';
import { CompanyProfileCard, CompanyEditDialog } from './CompanyProfileSection';
import { BranchOverview } from './BranchOverview';
import { BranchEditorDialog } from './BranchEditorDialog';
import { EMPTY_FORM, type BranchForm } from './branchForm';
import { AlertIcon } from './CompanyBranchesIcons';
import type { ConfirmKind, SuccessInfo } from './companyBranchesPresentation';
import styles from './CompanyBranchesPage.module.css';

export function CompanyBranchesPage() {
  const { user, setUser } = useAuth();
  const navigate = useNavigate();
  const caps = companyBranchesCapabilities(user);
  const notice = accessNotice(caps);

  const [modal, setModal] = useState<'create' | 'edit' | null>(null);
  const [successInfo, setSuccessInfo] = useState<SuccessInfo | null>(null);
  const [confirmKind, setConfirmKind] = useState<ConfirmKind | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [bForm, setBForm] = useState<BranchForm>(EMPTY_FORM);

  const [toast, setToast] = useState<{ msg: string; warn?: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showToast = useCallback((msg: string, warn = false) => {
    setToast({ msg, warn });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), warn ? 6000 : 3500);
  }, []);

  const onboarding = useBranchOnboarding(modal, bForm.time_zone_id);
  const company = useCompanyProfile({ user, setUser, setSuccessInfo, setConfirmKind });
  const branches = useBranchManagement({
    modal,
    setModal,
    onboarding,
    caps,
    user,
    setCompany: company.setCompany,
    setSuccessInfo,
    showToast,
    bForm,
    setBForm,
  });

  async function handleConfirm() {
    if (!confirmKind) return;
    setConfirming(true);
    try {
      if (confirmKind === 'save-company') {
        await company.saveCo();
      } else if (confirmKind === 'save-branch') {
        await branches.saveBranch();
      } else if (confirmKind === 'set-default' && branches.editingBranch) {
        await branches.setDefault(branches.editingBranch);
      }
    } finally {
      setConfirming(false);
      setConfirmKind(null);
    }
  }

  return (
    <div className={styles.page}>

      {/* Success dialog */}
      {successInfo && (
        <div className={styles.modalOverlay} onClick={() => setSuccessInfo(null)}>
          <div className={styles.successDialog} onClick={e => e.stopPropagation()}>
            <div className={styles.successIconWrap}>
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#16a34a" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" stroke="#bbf7d0" strokeWidth="1.5" fill="#f0fdf4" />
                <polyline points="20 6 9 17 4 12" stroke="#16a34a" strokeWidth="2.5" />
              </svg>
            </div>
            <h2 className={styles.successTitle}>{successInfo.title}</h2>
            <p className={styles.successSub}>{successInfo.sub}</p>
            {successInfo.detail && <p className={styles.successSub}>{successInfo.detail}</p>}
            {successInfo.code != null && (
              <p className={styles.successSub}><code className={styles.errorCode}>{successInfo.code}</code></p>
            )}
            {successInfo.links && successInfo.links.length > 0 && (
              <div className={styles.successLinks}>
                {successInfo.links.map((l) => (
                  <Link key={l.to} to={l.to} className={styles.scheduleLink} onClick={() => setSuccessInfo(null)}>
                    {l.label}
                  </Link>
                ))}
              </div>
            )}
            <button className={styles.btnPrimary} onClick={() => setSuccessInfo(null)}>Done</button>
          </div>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div
          className={`${styles.toast} ${toast.warn ? styles.toastError : ''}`}
          role="alert"
          aria-live="assertive"
        >
          {toast.warn && <AlertIcon />}
          {toast.msg}
        </div>
      )}

      {notice && (
        <ReadOnlyBanner
          tone="locked"
          title={caps.canCreate || caps.canAdmin ? 'Limited access' : 'View only'}
          message={notice}
        />
      )}

      <CompanyProfileCard
        company={company.company}
        companyLoading={company.companyLoading}
        companyError={company.companyError}
        canAdmin={caps.canAdmin}
        startEditCo={company.startEditCo}
      />

      <CompanyEditDialog
        editingCompany={company.editingCompany}
        company={company.company}
        setEditingCompany={company.setEditingCompany}
        savingCo={company.savingCo}
        coForm={company.coForm}
        setCoForm={company.setCoForm}
        currencies={company.currencies}
        currencyLoadError={company.currencyLoadError}
        coSaveErr={company.coSaveErr}
        requestSaveCo={company.requestSaveCo}
      />

      <BranchOverview
        branchesLoading={branches.branchesLoading}
        branches={branches.branches}
        filter={branches.filter}
        setFilter={branches.setFilter}
        searchQuery={branches.searchQuery}
        setSearchQuery={branches.setSearchQuery}
        filtered={branches.filtered}
        caps={caps}
        openCreate={branches.openCreate}
        branchesError={branches.branchesError}
        user={user}
        navigate={navigate}
        openEdit={branches.openEdit}
        kpi={branches.kpi}
      />

      <ConfirmDialog
        open={confirmKind !== null}
        title={
          confirmKind === 'save-company' ? 'Save company changes?' :
          confirmKind === 'set-default'  ? 'Set as default branch?' :
          'Save branch changes?'
        }
        message={
          confirmKind === 'save-company'
            ? 'This will update your company details.'
            : confirmKind === 'set-default'
            ? `Set "${branches.editingBranch?.branch_name}" as the default branch? This will replace the current default.`
            : modal === 'create'
            ? (caps.canOnboard && onboarding.setUpPayrollNow && onboarding.hasCompanyDefault && onboarding.firstPayrollStart.trim() !== ''
                ? `Create this new branch? The server will try to set up payroll for it, effective ${formatIsoLong(onboarding.firstPayrollStart.trim())}.`
                : 'Create this new branch?')
            : `Save changes to "${branches.editingBranch?.branch_name}"?`
        }
        confirmLabel={
          confirmKind === 'set-default' ? 'Set as Default' :
          confirmKind === 'save-company' || confirmKind === 'save-branch' ? 'Save' :
          'Confirm'
        }
        variant="primary"
        loading={confirming || company.savingCo || branches.savingBranch || branches.settingDefault}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />

      <BranchEditorDialog
        modal={modal}
        editingBranch={branches.editingBranch}
        closeModal={branches.closeModal}
        savingBranch={branches.savingBranch}
        caps={caps}
        settingDefault={branches.settingDefault}
        setConfirmKind={setConfirmKind}
        bForm={bForm}
        setBForm={setBForm}
        timeZones={branches.timeZones}
        onboarding={onboarding}
        bSaveErr={branches.bSaveErr}
        bCreateErr={branches.bCreateErr}
        canCreateNow={onboarding.canCreateNow}
      />

    </div>
  );
}
