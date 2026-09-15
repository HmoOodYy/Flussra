import { useEffect, useState, useMemo, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { canManageSettingsAdmin } from '../../../lib/permissions';
import type { CompanyProfile, CompanyUpdate, BranchAdmin } from '../../../types/settings';
import { CompanyStatusBadge, BranchStatusBadge } from '../../../components/StatusBadge';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { EmptyState, ErrorState, ReadOnlyBanner } from '../../../components/ui';
import styles from './CompanyBranchesPage.module.css';

// ─── Constants ────────────────────────────────────────────────────────────────

const BRANCH_STATUSES = ['Active', 'Inactive', 'Closed'] as const;
type BranchStatus = typeof BRANCH_STATUSES[number];

const COUNTRIES = [
  '', 'Egypt', 'United Arab Emirates', 'Saudi Arabia', 'Kuwait', 'Qatar',
  'Bahrain', 'Oman', 'Jordan', 'Lebanon', 'Iraq', 'Libya', 'Morocco', 'Tunisia',
  'United States', 'United Kingdom', 'Canada', 'Australia',
  'Germany', 'France', 'Netherlands', 'Turkey', 'Other',
];

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmtDate(dt: string | null): string {
  if (!dt) return '—';
  return new Date(dt).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0)
    return d.map((i: unknown) => i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null).filter(Boolean).join(' ');
  return 'An unexpected error occurred.';
}

// ─── Branch form ──────────────────────────────────────────────────────────────

interface BranchForm {
  branch_name: string; branch_code: string; status: BranchStatus;
  is_default: boolean; address_line1: string; city: string;
  state_province: string; postal_code: string; country: string; notes: string;
}

const EMPTY_FORM: BranchForm = {
  branch_name: '', branch_code: '', status: 'Active', is_default: false,
  address_line1: '', city: '', state_province: '', postal_code: '', country: '', notes: '',
};

function toForm(b: BranchAdmin): BranchForm {
  return {
    branch_name: b.branch_name, branch_code: b.branch_code, status: b.status as BranchStatus,
    is_default: b.is_default, address_line1: b.address_line1 ?? '',
    city: b.city ?? '', state_province: b.state_province ?? '',
    postal_code: b.postal_code ?? '', country: b.country ?? '', notes: b.notes ?? '',
  };
}

// ─── Main component ───────────────────────────────────────────────────────────

export function CompanyBranchesPage() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const canEdit = user !== null && canManageSettingsAdmin(user);

  // ── Company ──────────────────────────────────────────────────────────────
  const [company, setCompany]             = useState<CompanyProfile | null>(null);
  const [companyLoading, setCompanyLoading] = useState(true);
  const [companyError, setCompanyError]   = useState('');
  const [editingCompany, setEditingCompany] = useState(false);
  const [coForm, setCoForm]               = useState<CompanyUpdate>({ company_name: '' });
  const [savingCo, setSavingCo]           = useState(false);
  const [coSaveErr, setCoSaveErr]         = useState('');
  const [successInfo, setSuccessInfo]     = useState<{ title: string; sub: string } | null>(null);

  // ── Branches ──────────────────────────────────────────────────────────────
  const [branches, setBranches]           = useState<BranchAdmin[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(true);
  const [branchesError, setBranchesError] = useState('');
  const [filter, setFilter]               = useState<'All' | BranchStatus>('All');
  const [searchQuery, setSearchQuery]     = useState('');

  // ── Branch modal ──────────────────────────────────────────────────────────
  const [modal, setModal]                 = useState<'create' | 'edit' | null>(null);
  const [editingBranch, setEditingBranch] = useState<BranchAdmin | null>(null);
  const [bForm, setBForm]                 = useState<BranchForm>(EMPTY_FORM);
  const [savingBranch, setSavingBranch]   = useState(false);
  const [bSaveErr, setBSaveErr]           = useState('');
  const [settingDefault, setSettingDefault] = useState(false);

  // ── Confirmation dialog ───────────────────────────────────────────────────
  type ConfirmKind = 'save-company' | 'save-branch' | 'set-default';
  const [confirmKind, setConfirmKind] = useState<ConfirmKind | null>(null);
  const [confirming,  setConfirming]  = useState(false);

  // ── Toast ─────────────────────────────────────────────────────────────────
  const [toast, setToast] = useState<{ msg: string; warn?: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showToast = useCallback((msg: string, warn = false) => {
    setToast({ msg, warn });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), warn ? 6000 : 3500);
  }, []);

  // ── Load company ──────────────────────────────────────────────────────────
  useEffect(() => {
    apiClient.get<CompanyProfile>('/settings/company')
      .then(({ data }) => { setCompany(data); setCompanyLoading(false); })
      .catch((e) => { setCompanyError(apiError(e)); setCompanyLoading(false); });
  }, []);

  // ── Load branches ─────────────────────────────────────────────────────────
  useEffect(() => {
    apiClient.get<BranchAdmin[]>('/settings/branches')
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

  // ── KPI summary ───────────────────────────────────────────────────────────
  const kpi = useMemo(() => ({
    total:    branches.length,
    active:   branches.filter((b) => b.status === 'Active').length,
    inactive: branches.filter((b) => b.status === 'Inactive').length,
    closed:   branches.filter((b) => b.status === 'Closed').length,
    missingSetup:  branches.filter((b) => b.status === 'Active' && !b.payroll_setup_done).length,
    pending:       branches.reduce((s, b) => s + (b.pending_approvals_count ?? 0), 0),
    activeDrivers: branches.reduce((s, b) => s + (b.active_drivers_count ?? 0), 0),
  }), [branches]);

  // ── Company edit ──────────────────────────────────────────────────────────
  function startEditCo() {
    if (!company) return;
    setCoForm({ company_name: company.company_name, legal_name: company.legal_name,
      timezone_name: company.timezone_name, notes: company.notes, allow_self_approval: company.allow_self_approval });
    setCoSaveErr('');
    setEditingCompany(true);
  }

  /** Validate then open confirm dialog — actual save happens in handleConfirm. */
  function requestSaveCo() {
    if (!coForm.company_name.trim()) { setCoSaveErr('Company name is required.'); return; }
    setConfirmKind('save-company');
  }

  async function saveCo() {
    setSavingCo(true); setCoSaveErr('');
    try {
      const { data } = await apiClient.patch<CompanyProfile>('/settings/company', coForm);
      setCompany(data); setEditingCompany(false);
      setSuccessInfo({ title: 'Company Updated!', sub: 'Your company details have been saved.' });
    } catch (e) { setCoSaveErr(apiError(e)); }
    finally { setSavingCo(false); }
  }

  async function setDefault(b: BranchAdmin) {
    if (b.is_default) return;
    setSettingDefault(true);
    try {
      const { data } = await apiClient.post<BranchAdmin>(
        `/settings/branches/${b.branch_id}/set-default`);
      setBranches((p) => p.map((br) => ({ ...br, is_default: br.branch_id === data.branch_id })));
      // Refresh company profile to update default_branch_name
      apiClient.get<CompanyProfile>('/settings/company')
        .then(({ data: co }) => setCompany(co)).catch(() => {});
      closeModal();
      setSuccessInfo({ title: 'Default Branch Set!', sub: `"${data.branch_name}" is now the default branch.` });
    } catch (e) { showToast(apiError(e) || 'Could not set default branch. Please try again.', true); }
    finally { setSettingDefault(false); }
  }

  /** Handles confirm dialog "OK" for any action type. */
  async function handleConfirm() {
    if (!confirmKind) return;
    setConfirming(true);
    try {
      if (confirmKind === 'save-company') {
        await saveCo();
      } else if (confirmKind === 'save-branch') {
        await saveBranch();
      } else if (confirmKind === 'set-default' && editingBranch) {
        await setDefault(editingBranch);
      }
    } finally {
      setConfirming(false);
      setConfirmKind(null);
    }
  }

  // ── Branch modal ──────────────────────────────────────────────────────────
  function openCreate() { setBForm(EMPTY_FORM); setBSaveErr(''); setEditingBranch(null); setModal('create'); }

  function openEdit(b: BranchAdmin) {
    setBForm(toForm(b)); setBSaveErr(''); setEditingBranch(b);
    setModal('edit');
  }

  function closeModal() { setModal(null); setEditingBranch(null); setBSaveErr(''); }

  async function saveBranch() {
    if (!bForm.branch_name.trim()) { setBSaveErr('Branch name is required.'); return; }
    setSavingBranch(true); setBSaveErr('');
    try {
      const payload = {
        branch_name:    bForm.branch_name.trim(),
        branch_code:    bForm.branch_code.trim() || undefined,
        status:         bForm.status,
        ...(modal === 'create' ? { is_default: bForm.is_default } : {}),
        address_line1:  bForm.address_line1 || null,
        city:           bForm.city || null,
        state_province: bForm.state_province || null,
        postal_code:    bForm.postal_code || null,
        country:        bForm.country || null,
        notes:          bForm.notes || null,
      };
      if (modal === 'create') {
        const { data } = await apiClient.post<BranchAdmin>('/settings/branches', payload);
        setBranches((p) => [...p, data]);
        closeModal();
        setSuccessInfo({ title: `Branch Created!`, sub: `"${data.branch_name}" has been successfully added.` });
      } else if (editingBranch) {
        const { data } = await apiClient.patch<BranchAdmin>(`/settings/branches/${editingBranch.branch_id}`, payload);
        setBranches((p) => p.map((b) => b.branch_id === data.branch_id ? data : b));
        closeModal();
        setSuccessInfo({ title: `Branch Updated!`, sub: `"${data.branch_name}" has been saved successfully.` });
      }
    } catch (e) { setBSaveErr(apiError(e)); }
    finally { setSavingBranch(false); }
  }

  // ─────────────────────────────────────────────────────────────────────────
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

      {!canEdit && (
        <ReadOnlyBanner
          tone="locked"
          title="View only"
          message="You don't have permission to edit company or branch settings."
        />
      )}

      {/* ── Company card ── */}
      {companyLoading ? (
        <div className={styles.companyCard}><SkeletonBlock /></div>
      ) : companyError ? (
        <ErrorState message={companyError} />
      ) : company && (
        <div className={styles.companyCard}>
          <div className={styles.companyCardTop}>
            <div className={styles.companyIdentity}>
              <div className={styles.companyAvatar}>
                {company.company_name.slice(0, 2).toUpperCase()}
              </div>
              <div className={styles.companyNameGroup}>
                <span className={styles.companyNameText}>{company.company_name}</span>
                <div className={styles.companyBadgeRow}>
                  <CompanyStatusBadge status={company.status} isSuspended={company.is_suspended} />
                  {company.legal_name && (
                    <span style={{ fontSize: '0.78rem', color: '#6b7280' }}>
                      {company.legal_name}
                    </span>
                  )}
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              {canEdit && (
                <button className={styles.btnSecondary} onClick={startEditCo}>
                  <EditIcon /> Edit Company
                </button>
              )}
            </div>
          </div>

          <div className={styles.companyMeta}>
            <MetaItem label="Company Code"   value={company.company_code} />
            <MetaItem label="Timezone"        value={company.timezone_name} />
            <MetaItem label="Default Branch"  value={company.default_branch_name ?? '—'} />
            <MetaItem label="Self-Approval"   value={company.allow_self_approval ? 'Allowed' : 'Disabled'} />
            <MetaItem label="Created"         value={fmtDate(company.created_at_utc)} />
            <MetaItem label="Last Updated"    value={fmtDate(company.updated_at_utc)} />
            {company.notes && (
              <MetaItem label="Notes" value={company.notes} wide />
            )}
          </div>
        </div>
      )}

      {/* ── Company edit modal ── */}
      {editingCompany && company && (
        <div className={styles.modalOverlay} onClick={(e) => { if (e.target === e.currentTarget) setEditingCompany(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>Edit Company Details</h2>
              <button className={styles.modalCloseBtn} onClick={() => setEditingCompany(false)} disabled={savingCo} aria-label="Close">✕</button>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.formGrid}>
                <div className={styles.formGroup}>
                  <label className={styles.label}>Company Name <span className={styles.required}>*</span></label>
                  <input className={styles.input} value={coForm.company_name}
                    onChange={(e) => setCoForm((f) => ({ ...f, company_name: e.target.value }))}
                    disabled={savingCo} maxLength={120} autoFocus />
                </div>
                <div className={styles.formGroup}>
                  <label className={styles.label}>Legal Name</label>
                  <input className={styles.input} value={coForm.legal_name ?? ''}
                    onChange={(e) => setCoForm((f) => ({ ...f, legal_name: e.target.value || null }))}
                    disabled={savingCo} placeholder="Optional" maxLength={200} />
                </div>
                <div className={styles.formGroup}>
                  <label className={styles.label}>Timezone</label>
                  <input className={styles.input} value={coForm.timezone_name ?? ''}
                    onChange={(e) => setCoForm((f) => ({ ...f, timezone_name: e.target.value || null }))}
                    disabled={savingCo} placeholder="e.g. Africa/Cairo" maxLength={80} />
                </div>
                <div className={styles.formGroupFull}>
                  <label className={styles.label}>Notes</label>
                  <textarea className={styles.textarea} value={coForm.notes ?? ''}
                    onChange={(e) => setCoForm((f) => ({ ...f, notes: e.target.value || null }))}
                    disabled={savingCo} placeholder="Optional internal notes" maxLength={1000} />
                </div>
              </div>
              <div className={styles.toggleRow}>
                <div className={styles.toggleInfo}>
                  <span className={styles.toggleLabel}>Allow Self-Approval</span>
                  <span className={styles.toggleDesc}>
                    When off, the user who submitted a review item cannot also approve it.
                  </span>
                </div>
                <label className={styles.switch}>
                  <input type="checkbox" checked={coForm.allow_self_approval ?? true}
                    onChange={(e) => setCoForm((f) => ({ ...f, allow_self_approval: e.target.checked }))}
                    disabled={savingCo} />
                  <span className={styles.switchTrack} />
                </label>
              </div>
              {coSaveErr && <div className={styles.errorAlert} style={{ marginTop: '0.75rem' }}><AlertIcon /> {coSaveErr}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} onClick={requestSaveCo} disabled={savingCo}>
                {savingCo ? <><SpinnerIcon /> Saving…</> : 'Save Changes'}
              </button>
              <button className={styles.btnSecondary} onClick={() => setEditingCompany(false)} disabled={savingCo}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Two-column body ── */}
      <div className={styles.body}>

        {/* LEFT — Branches */}
        <div className={styles.branchesCol}>
          <div className={styles.sectionHeader}>
            {/* Left: title / count / hint */}
            <div className={styles.sectionLeft}>
              <h2 className={styles.sectionTitle}>Branches</h2>
              {!branchesLoading && branches.length > 0 && (
                <span className={styles.countChip}>
                  {(filter !== 'All' || searchQuery.trim()) && filtered.length !== branches.length
                    ? `${filtered.length} of ${branches.length}`
                    : branches.length}
                </span>
              )}
            </div>
            {/* Right: search + status filters + add */}
            <div className={styles.sectionRight}>
              <div className={styles.searchWrap}>
                <SearchIcon />
                <input
                  className={styles.searchInput}
                  type="search"
                  placeholder="Search branches…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                />
              </div>
              <div className={styles.filters}>
                {(['All', 'Active', 'Inactive', 'Closed'] as const).map((s) => (
                  <button key={s}
                    className={`${styles.filterBtn}${filter === s ? ` ${styles.filterBtnActive}` : ''}`}
                    onClick={() => setFilter(s)}>
                    {s}
                  </button>
                ))}
              </div>
              {canEdit && (
                <button className={styles.btnPrimary} onClick={openCreate}>
                  <PlusIcon /> Add Branch
                </button>
              )}
            </div>
          </div>

          {branchesLoading ? (
            <div className={styles.tableWrap}>
              <div style={{ padding: '1.5rem' }}><SkeletonBlock /></div>
            </div>
          ) : branchesError ? (
            <ErrorState message={branchesError} />
          ) : filtered.length === 0 ? (
            <div className={styles.tableWrap}>
              <EmptyState
                title={
                  searchQuery.trim()
                    ? `No branches match "${searchQuery.trim()}".`
                    : `No ${filter !== 'All' ? filter.toLowerCase() + ' ' : ''}branches found.`
                }
              />
            </div>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <colgroup>
                  <col style={{ width: '26%' }} />
                  <col style={{ width: '10%' }} />
                  <col style={{ width: '10%' }} />
                  <col style={{ width: '14%' }} />
                  <col style={{ width: '7%'  }} />
                  <col style={{ width: '7%'  }} />
                  <col style={{ width: '7%'  }} />
                  <col style={{ width: '13%' }} />
                  {canEdit && <col style={{ width: '6%' }} />}
                </colgroup>
                <thead>
                  <tr>
                    <th>Branch</th>
                    <th>Status</th>
                    <th>Default</th>
                    <th>Payroll Status</th>
                    <th className={styles.thCenter}>Keys</th>
                    <th className={styles.thCenter}>Drivers</th>
                    <th className={styles.thCenter}>Approvals</th>
                    <th>Location</th>
                    {canEdit && <th></th>}
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((b) => (
                    <tr
                      key={b.branch_id}
                      className={styles.tableRow}
                    >
                      <td>
                        <div className={styles.branchNameCell}>
                          <span className={styles.branchName}>{b.branch_name}</span>
                        </div>
                      </td>
                      <td><BranchStatusBadge status={b.status} /></td>
                      <td>
                        {b.is_default && (
                          <span className={styles.defaultBadge}><StarIcon /> Default</span>
                        )}
                      </td>
                      <td>
                        {b.payroll_setup_done
                          ? <span className={`${styles.setupBadge} ${styles.setupDone}`}><CheckIcon /> Complete</span>
                          : (
                            <button
                              type="button"
                              className={`${styles.setupBadge} ${styles.setupMissing} ${styles.setupMissingLink}`}
                              title="Open payroll setup for this branch"
                              aria-label={`Open payroll setup for ${b.branch_name}`}
                              onClick={e => {
                                e.stopPropagation(); // don't trigger row double-click
                                navigate(`/settings/payroll?branchId=${b.branch_id}&tab=pay-schedule`);
                              }}
                            >
                              <WarnIcon /> Setup Needed
                            </button>
                          )
                        }
                      </td>
                      <td className={styles.tdCenter}>{b.status_keys_count ?? '—'}</td>
                      <td className={styles.tdCenter}>{b.active_drivers_count ?? '—'}</td>
                      <td className={styles.tdCenter}>
                        {(b.pending_approvals_count ?? 0) > 0
                          ? <span style={{ color: '#d97706', fontWeight: 600 }}>{b.pending_approvals_count}</span>
                          : '—'
                        }
                      </td>
                      <td className={styles.tdLocation}>
                        {[b.city, b.country].filter(Boolean).join(', ') || '—'}
                      </td>
                      {canEdit && (
                        <td className={styles.tdAction}>
                          <button
                            className={styles.rowActionBtn}
                            onClick={() => openEdit(b)}
                            title="Edit branch"
                            aria-label={`Edit ${b.branch_name}`}
                          >⋮</button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* RIGHT — KPI panel */}
        <aside className={styles.kpiCol}>
          <div className={styles.kpiHeader}>
            <h2 className={styles.kpiTitle}>Summary</h2>
          </div>
          <div className={styles.kpiList}>
            <KpiRow label="Total Branches"  value={kpi.total} />
            <KpiRow label="Active"          value={kpi.active}   color={kpi.active > 0 ? 'good' : undefined} />
            <KpiRow label="Inactive"        value={kpi.inactive} color={kpi.inactive > 0 ? 'warn' : undefined} />
            <KpiRow label="Closed"          value={kpi.closed}   color={kpi.closed > 0 ? 'warn' : undefined} />
            <div className={styles.kpiItemDivider} />
            <KpiRow label="Needs Setup"     value={kpi.missingSetup}  color={kpi.missingSetup > 0 ? 'danger' : 'good'} />
            <KpiRow label="Pending Approvals" value={kpi.pending}     color={kpi.pending > 0 ? 'warn' : undefined} />
            <div className={styles.kpiItemDivider} />
            <KpiRow label="Active Drivers"  value={kpi.activeDrivers} />
          </div>
        </aside>

      </div>

      {/* ── Confirm dialog ── */}
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
            ? `Set "${editingBranch?.branch_name}" as the default branch? This will replace the current default.`
            : modal === 'create'
            ? 'Create this new branch?'
            : `Save changes to "${editingBranch?.branch_name}"?`
        }
        confirmLabel={
          confirmKind === 'set-default' ? 'Set as Default' :
          confirmKind === 'save-company' || confirmKind === 'save-branch' ? 'Save' :
          'Confirm'
        }
        variant="primary"
        loading={confirming || savingCo || savingBranch || settingDefault}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />

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
                  ) : editingBranch.status === 'Active' && (
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
                styles={styles}
              />
              {bSaveErr && <div className={styles.errorAlert} style={{ marginTop: '0.75rem' }}><AlertIcon /> {bSaveErr}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button
                className={styles.btnPrimary}
                onClick={() => setConfirmKind('save-branch')}
                disabled={savingBranch}
              >
                {savingBranch ? <><SpinnerIcon /> Saving…</> : modal === 'create' ? 'Create Branch' : 'Save Changes'}
              </button>
              <button className={styles.btnSecondary} onClick={closeModal} disabled={savingBranch}>Cancel</button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

// ─── Custom status picker ──────────────────────────────────────────────────────

const STATUS_META: Record<string, { color: string; bg: string; border: string }> = {
  Active:   { color: '#15803d', bg: '#f0fdf4', border: '#86efac' },
  Inactive: { color: '#92400e', bg: '#fffbeb', border: '#fcd34d' },
  Closed:   { color: '#6b7280', bg: '#f9fafb', border: '#d1d5db' },
};

function StatusPicker({ value, onChange, disabled }: {
  value: string; onChange: (v: string) => void; disabled: boolean; s: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const [dropStyle, setDropStyle] = useState<React.CSSProperties>({});
  const btnRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const meta = STATUS_META[value] ?? STATUS_META['Active'];

  const handleOpen = () => {
    if (disabled) return;
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setDropStyle({ position: 'fixed', top: r.bottom + 6, left: r.left, width: r.width, zIndex: 9999 });
    }
    setOpen(o => !o);
  };

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  return (
    <div ref={wrapRef}>
      <button
        ref={btnRef}
        type="button"
        disabled={disabled}
        onClick={handleOpen}
        style={{
          width: '100%', height: '40px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 0.75rem', border: `1.5px solid ${open ? '#3b82f6' : '#e2e8f0'}`,
          borderRadius: '8px', background: '#fff', cursor: disabled ? 'not-allowed' : 'pointer',
          boxShadow: open ? '0 0 0 3px rgba(59,130,246,0.12)' : 'none',
          transition: 'border-color 0.14s, box-shadow 0.14s', fontFamily: 'inherit',
        }}
      >
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: '0.5rem',
          fontSize: '0.875rem', fontWeight: 600, color: meta.color,
          background: meta.bg, border: `1px solid ${meta.border}`,
          borderRadius: '20px', padding: '0.2rem 0.75rem',
        }}>
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: meta.color, flexShrink: 0 }} />
          {value}
        </span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {open && (
        <div style={{
          ...dropStyle,
          background: '#fff', border: '1px solid #e2e8f0', borderRadius: '10px',
          boxShadow: '0 8px 24px rgba(15,23,42,0.12)', overflow: 'hidden',
        }}>
          {BRANCH_STATUSES.map(st => {
            const m = STATUS_META[st];
            const isSelected = st === value;
            return (
              <button
                key={st} type="button"
                onClick={() => { onChange(st); setOpen(false); }}
                style={{
                  width: '100%', display: 'flex', alignItems: 'center', gap: '0.6rem',
                  padding: '0.65rem 0.9rem', background: isSelected ? '#f0f9ff' : 'transparent',
                  border: 'none', cursor: 'pointer', textAlign: 'left', fontFamily: 'inherit',
                  borderBottom: st !== 'Closed' ? '1px solid #f1f5f9' : 'none',
                }}
                onMouseEnter={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = '#f8fafc'; }}
                onMouseLeave={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = isSelected ? '#f0f9ff' : 'transparent'; }}
              >
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: m.color, flexShrink: 0 }} />
                <span style={{ fontSize: '0.875rem', fontWeight: isSelected ? 700 : 500, color: isSelected ? m.color : '#374151' }}>{st}</span>
                {isSelected && (
                  <svg style={{ marginLeft: 'auto', color: m.color }} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Custom country picker ─────────────────────────────────────────────────────

function CountryPicker({ value, onChange, disabled }: {
  value: string; onChange: (v: string) => void; disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [dropStyle, setDropStyle] = useState<React.CSSProperties>({});
  const btnRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const handleOpen = () => {
    if (disabled) return;
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setDropStyle({ position: 'fixed', top: r.bottom + 6, left: r.left, width: r.width, zIndex: 9999 });
    }
    setOpen(o => !o);
  };

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const options = COUNTRIES.filter(c => c !== '');

  return (
    <div ref={wrapRef}>
      <button
        ref={btnRef} type="button" disabled={disabled} onClick={handleOpen}
        style={{
          width: '100%', height: '40px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 0.75rem', border: `1.5px solid ${open ? '#3b82f6' : '#e2e8f0'}`,
          borderRadius: '8px', background: '#fff', cursor: disabled ? 'not-allowed' : 'pointer',
          boxShadow: open ? '0 0 0 3px rgba(59,130,246,0.12)' : 'none',
          transition: 'border-color 0.14s, box-shadow 0.14s', fontFamily: 'inherit',
          fontSize: '0.875rem', color: value ? '#0f172a' : '#94a3b8',
        }}
      >
        <span>{value || '— Select country —'}</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {open && (
        <div style={{
          ...dropStyle,
          background: '#fff', border: '1px solid #e2e8f0', borderRadius: '10px',
          boxShadow: '0 8px 24px rgba(15,23,42,0.12)', overflow: 'hidden',
          maxHeight: 220, overflowY: 'auto',
        }}>
          {value && (
            <button type="button"
              onClick={() => { onChange(''); setOpen(false); }}
              style={{ width: '100%', display: 'flex', alignItems: 'center', padding: '0.6rem 0.9rem', background: 'transparent', border: 'none', borderBottom: '1px solid #f1f5f9', cursor: 'pointer', fontFamily: 'inherit', fontSize: '0.8rem', color: '#94a3b8', textAlign: 'left' }}
            >Clear selection</button>
          )}
          {options.map(c => {
            const isSelected = c === value;
            return (
              <button key={c} type="button"
                onClick={() => { onChange(c); setOpen(false); }}
                style={{
                  width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '0.6rem 0.9rem', background: isSelected ? '#f0f9ff' : 'transparent',
                  border: 'none', borderBottom: '1px solid #f8fafc', cursor: 'pointer',
                  fontFamily: 'inherit', fontSize: '0.875rem',
                  fontWeight: isSelected ? 700 : 400, color: isSelected ? '#1d4ed8' : '#374151',
                  textAlign: 'left',
                }}
                onMouseEnter={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = '#f8fafc'; }}
                onMouseLeave={e => { if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = 'transparent'; }}
              >
                {c}
                {isSelected && <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Branch form fields ────────────────────────────────────────────────────────

function BranchFormFields({
  form, onChange, disabled, isCreate, styles: s,
}: {
  form: BranchForm;
  onChange: (p: Partial<BranchForm>) => void;
  disabled: boolean;
  isCreate: boolean;
  styles: Record<string, string>;
}) {
  return (
    <div className={s.formGrid}>
      {/* Branch Name — full width */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Branch Name <span className={s.required}>*</span></label>
        <input className={s.input} value={form.branch_name}
          onChange={(e) => onChange({ branch_name: e.target.value })}
          disabled={disabled} maxLength={120} autoFocus />
      </div>
      {/* Status (half) | Country (half) */}
      <div className={s.formGroup}>
        <label className={s.label}>Status</label>
        <StatusPicker value={form.status} onChange={(v) => onChange({ status: v as BranchForm['status'] })} disabled={disabled} s={s} />
      </div>
      <div className={s.formGroup}>
        <label className={s.label}>Country</label>
        <CountryPicker value={form.country} onChange={(v) => onChange({ country: v })} disabled={disabled} />
      </div>
      {/* Address — full width */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Address</label>
        <input className={s.input} value={form.address_line1}
          onChange={(e) => onChange({ address_line1: e.target.value })}
          disabled={disabled} placeholder="Street address" maxLength={200} />
      </div>
      {/* City | State/Province */}
      <div className={s.formGroup}>
        <label className={s.label}>City</label>
        <input className={s.input} value={form.city}
          onChange={(e) => onChange({ city: e.target.value })} disabled={disabled} maxLength={100} />
      </div>
      <div className={s.formGroup}>
        <label className={s.label}>State / Province</label>
        <input className={s.input} value={form.state_province}
          onChange={(e) => onChange({ state_province: e.target.value })} disabled={disabled} maxLength={100} />
      </div>
      {/* Postal Code — half */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Postal Code</label>
        <input className={s.input} value={form.postal_code}
          onChange={(e) => onChange({ postal_code: e.target.value })} disabled={disabled} maxLength={20} />
      </div>
      {/* Notes — full width */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Notes</label>
        <textarea className={s.textarea} value={form.notes}
          onChange={(e) => onChange({ notes: e.target.value })}
          disabled={disabled} placeholder="Optional" maxLength={500} />
      </div>
      {isCreate && (
        <div className={`${s.formGroup} ${s.formGroupFull}`}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem',
            cursor: 'pointer', fontSize: '0.875rem', fontWeight: 500, color: '#374151' }}>
            <input type="checkbox" checked={form.is_default}
              onChange={(e) => onChange({ is_default: e.target.checked })}
              disabled={disabled} style={{ accentColor: '#2563eb', width: 16, height: 16 }} />
            Set as default branch
          </label>
        </div>
      )}
    </div>
  );
}

// ─── Small components ─────────────────────────────────────────────────────────

function MetaItem({ label, value, wide }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={styles.metaItem} style={wide ? { gridColumn: '1 / -1' } : undefined}>
      <span className={styles.metaLabel}>{label}</span>
      <span className={styles.metaValue}>{value}</span>
    </div>
  );
}

function KpiRow({ label, value, color }: { label: string; value: number; color?: 'good' | 'warn' | 'danger' }) {
  const cls = color === 'good' ? styles.kpiValueGood
    : color === 'warn' ? styles.kpiValueWarn
    : color === 'danger' ? styles.kpiValueDanger
    : styles.kpiValue;
  return (
    <div className={styles.kpiItem}>
      <span className={styles.kpiLabel}>{label}</span>
      <span className={cls}>{value}</span>
    </div>
  );
}

function SkeletonBlock() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.65rem' }}>
      {[55, 38, 72, 30].map((w, i) => (
        <div key={i} className={styles.skeleton} style={{ width: `${w}%` }} />
      ))}
    </div>
  );
}

// ─── Icons ────────────────────────────────────────────────────────────────────

function PlusIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>;
}
function EditIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>;
}
function CheckIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>;
}
function AlertIcon() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>;
}
function WarnIcon() {
  return <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>;
}
function StarIcon() {
  return <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>;
}
function CloseIcon() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>;
}
function SpinnerIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" className={styles.spinner} aria-hidden="true"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>;
}
function SearchIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={styles.searchIcon} aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>;
}
