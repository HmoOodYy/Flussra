import { useEffect, useMemo, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { UserProfile } from '../../../store/authStore';
import type { BranchAdmin, CompanyProfile } from '../../../types/settings';
import { createSettingsBranch, getCompanyProfile, getSettingsBranches, setDefaultSettingsBranch, updateSettingsBranch } from '../../../lib/settingsApi';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { CompanyBranchesCapabilities } from './companyBranchesView';
import { buildBranchCreatePayload, buildBranchUpdatePayload, onboardingResult, branchScheduleLink } from './companyBranchesView';
import { apiError, type SuccessInfo } from './companyBranchesPresentation';
import { EMPTY_FORM, toForm, type BranchForm } from './branchForm';
import type { BranchOnboardingState } from './useBranchOnboarding';

export function useBranchManagement({
  modal,
  setModal,
  onboarding,
  caps,
  user,
  setCompany,
  setSuccessInfo,
  showToast,
}: {
  modal: 'create' | 'edit' | null;
  setModal: Dispatch<SetStateAction<'create' | 'edit' | null>>;
  onboarding: BranchOnboardingState;
  caps: CompanyBranchesCapabilities;
  user: UserProfile | null;
  setCompany: Dispatch<SetStateAction<CompanyProfile | null>>;
  setSuccessInfo: Dispatch<SetStateAction<SuccessInfo | null>>;
  showToast: (msg: string, warn?: boolean) => void;
}) {
  const [branches, setBranches] = useState<BranchAdmin[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(true);
  const [branchesError, setBranchesError] = useState('');
  const [filter, setFilter] = useState<'All' | 'Active' | 'Inactive' | 'Closed'>('All');
  const [searchQuery, setSearchQuery] = useState('');

  const [editingBranch, setEditingBranch] = useState<BranchAdmin | null>(null);
  const [bForm, setBForm] = useState<BranchForm>(EMPTY_FORM);
  const [savingBranch, setSavingBranch] = useState(false);
  const [bSaveErr, setBSaveErr] = useState('');
  const [bCreateErr, setBCreateErr] = useState<ApiErrorInfo | null>(null);
  const [settingDefault, setSettingDefault] = useState(false);

  useEffect(() => {
    getSettingsBranches()
      .then(({ data }) => { setBranches(data); setBranchesLoading(false); })
      .catch((e) => { setBranchesError(apiError(e)); setBranchesLoading(false); });
  }, []);

  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return branches.filter((b) => {
      const matchStatus = filter === 'All' || b.status === filter;
      if (!matchStatus) return false;
      if (!q) return true;
      const location = [b.city, b.country].filter(Boolean).join(', ').toLowerCase();
      return (
        b.branch_name.toLowerCase().includes(q) ||
        b.branch_code.toLowerCase().includes(q) ||
        location.includes(q)
      );
    });
  }, [branches, filter, searchQuery]);

  const kpi = useMemo(() => ({
    total:    branches.length,
    active:   branches.filter((b) => b.status === 'Active').length,
    inactive: branches.filter((b) => b.status === 'Inactive').length,
    closed:   branches.filter((b) => b.status === 'Closed').length,
    missingSetup:  branches.filter((b) => b.status === 'Active' && !b.payroll_setup_done).length,
    pending:       branches.reduce((s, b) => s + (b.pending_approvals_count ?? 0), 0),
    activeDrivers: branches.reduce((s, b) => s + (b.active_drivers_count ?? 0), 0),
  }), [branches]);

  async function setDefault(b: BranchAdmin) {
    if (b.is_default) return;
    setSettingDefault(true);
    try {
      const { data } = await setDefaultSettingsBranch(b.branch_id);
      setBranches((p) => p.map((br) => ({ ...br, is_default: br.branch_id === data.branch_id })));
      // Refresh company profile to update default_branch_name
      getCompanyProfile().then(({ data: co }) => setCompany(co)).catch(() => {});
      closeModal();
      setSuccessInfo({ title: 'Default Branch Set!', sub: `"${data.branch_name}" is now the default branch.` });
    } catch (e) { showToast(apiError(e) || 'Could not set default branch. Please try again.', true); }
    finally { setSettingDefault(false); }
  }

  function openCreate() {
    setBForm(EMPTY_FORM); setBSaveErr(''); setBCreateErr(null); onboarding.setFirstPayrollStart('');
    onboarding.setSetUpPayrollNow(false); onboarding.setOnboardingDefaultSetup(null); onboarding.setOnboardingDefaultError(null);
    setEditingBranch(null); setModal('create');
  }

  function openEdit(b: BranchAdmin) {
    setBForm(toForm(b)); setBSaveErr(''); setEditingBranch(b);
    setModal('edit');
  }

  function closeModal() {
    setModal(null); setEditingBranch(null); setBSaveErr(''); setBCreateErr(null); onboarding.setFirstPayrollStart('');
    onboarding.setSetUpPayrollNow(false); onboarding.setOnboardingDefaultSetup(null); onboarding.setOnboardingDefaultError(null);
  }

  async function saveBranch() {
    if (!bForm.branch_name.trim()) { setBSaveErr('Branch name is required.'); return; }
    setSavingBranch(true); setBSaveErr(''); setBCreateErr(null);
    try {
      if (modal === 'create') {
        const payload = buildBranchCreatePayload(bForm, onboarding.firstPayrollStart, {
          canOnboard: caps.canOnboard,
          setUpPayroll: onboarding.setUpPayrollNow,
          canSetDefaultOnCreate: caps.canSetDefaultOnCreate,
        });
        const submittedOnboarding = 'first_payroll_start_date' in payload;
        const { data } = await createSettingsBranch(payload);
        setBranches((p) => [...p, data]);
        closeModal();
        const links: { to: string; label: string }[] = [];
        const scheduleHref = branchScheduleLink(user, data.branch_id);
        if (scheduleHref) links.push({ to: scheduleHref, label: 'View payroll schedule' });
        if (caps.canViewSetups) links.push({ to: '/settings/payroll', label: 'Open Payroll Setups' });

        if (!submittedOnboarding) {
          setSuccessInfo({
            title: `Branch Created!`,
            sub: `"${data.branch_name}" has been successfully added.`,
            links,
          });
        } else {
          const result = onboardingResult(data);
          setSuccessInfo({
            title: 'Branch Created!',
            sub: result.message,
            detail: result.evaluatedDate != null ? `Evaluated payroll start: ${result.evaluatedDate}` : undefined,
            code: result.tone !== 'ready' && result.code != null ? result.code : undefined,
            links,
          });
        }
      } else if (editingBranch) {
        const payload = buildBranchUpdatePayload(bForm);
        const { data } = await updateSettingsBranch(editingBranch.branch_id, payload);
        setBranches((p) => p.map((b) => b.branch_id === data.branch_id ? data : b));
        closeModal();
        setSuccessInfo({ title: `Branch Updated!`, sub: `"${data.branch_name}" has been saved successfully.` });
      }
    } catch (e) {
      if (modal === 'create') setBCreateErr(readApiError(e, 'Could not create the Branch.'));
      else setBSaveErr(apiError(e));
    }
    finally { setSavingBranch(false); }
  }

  return {
    branches,
    branchesLoading,
    branchesError,
    filter,
    setFilter,
    searchQuery,
    setSearchQuery,
    filtered,
    kpi,
    editingBranch,
    bForm,
    setBForm,
    savingBranch,
    bSaveErr,
    bCreateErr,
    settingDefault,
    setDefault,
    openCreate,
    openEdit,
    closeModal,
    saveBranch,
  };
}
