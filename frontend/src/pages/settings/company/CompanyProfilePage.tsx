import { useEffect, useReducer, useState } from 'react';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import type { CompanyProfile, CompanyUpdate } from '../../../types/settings';
import { CompanyStatusBadge } from '../../../components/StatusBadge';
import styles from './CompanyProfilePage.module.css';

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmt(dt: string | null): string {
  if (!dt) return '—';
  return new Date(dt).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  });
}

function apiError(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((i: unknown) =>
        i && typeof i === 'object' && 'msg' in i
          ? String((i as { msg: unknown }).msg)
          : null
      )
      .filter(Boolean)
      .join(' ');
  }
  return 'An unexpected error occurred. Please try again.';
}

// ─── Load-state reducer ───────────────────────────────────────────────────────

type LoadState = { loading: boolean; error: string; profile: CompanyProfile | null };
type LoadAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    profile: CompanyProfile }
  | { type: 'FETCH_ERROR'; error: string };

function loadReducer(s: LoadState, a: LoadAction): LoadState {
  switch (a.type) {
    case 'FETCH_START': return { loading: true,  error: '',       profile: s.profile };
    case 'FETCH_OK':    return { loading: false, error: '',       profile: a.profile };
    case 'FETCH_ERROR': return { loading: false, error: a.error,  profile: null      };
    default:            return s;
  }
}

// ─── Component ────────────────────────────────────────────────────────────────

export function CompanyProfilePage() {
  const { user } = useAuth();
  // AllCompanyBranches scope = admin who can edit; SpecificBranch = read-only
  const canEdit = user?.scope_type === 'AllCompanyBranches';

  const [loadSt, dispatchLoad] = useReducer(loadReducer, { loading: true, error: '', profile: null });

  const [editing, setEditing]     = useState(false);
  const [form, setForm]           = useState<CompanyUpdate>({ company_name: '' });
  const [saving, setSaving]       = useState(false);
  const [saveError, setSaveError] = useState('');
  const [saved, setSaved]         = useState(false);

  // ── Load ──────────────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    dispatchLoad({ type: 'FETCH_START' });

    apiClient.get<CompanyProfile>('/settings/company')
      .then(({ data }) => {
        if (!cancelled) dispatchLoad({ type: 'FETCH_OK', profile: data });
      })
      .catch((err) => {
        if (!cancelled) dispatchLoad({ type: 'FETCH_ERROR', error: apiError(err) });
      });

    return () => { cancelled = true; };
  }, []);

  // ── Edit ──────────────────────────────────────────────────────────────────
  function startEdit() {
    const profile = loadSt.profile;
    if (!profile) return;
    setForm({
      company_name:        profile.company_name,
      legal_name:          profile.legal_name,
      timezone_name:       profile.timezone_name,
      notes:               profile.notes,
      allow_self_approval: profile.allow_self_approval,
    });
    setSaveError('');
    setSaved(false);
    setEditing(true);
  }

  function cancelEdit() { setEditing(false); setSaveError(''); }

  // ── Save ──────────────────────────────────────────────────────────────────
  async function handleSave() {
    if (!form.company_name.trim()) { setSaveError('Company name is required.'); return; }
    setSaving(true); setSaveError(''); setSaved(false);
    try {
      const { data } = await apiClient.patch<CompanyProfile>('/settings/company', form);
      dispatchLoad({ type: 'FETCH_OK', profile: data });
      setEditing(false);
      setSaved(true);
      setTimeout(() => setSaved(false), 4000);
    } catch (err) {
      setSaveError(apiError(err));
    } finally {
      setSaving(false);
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>

      {/* Suspended banner */}
      {loadSt.profile?.is_suspended && (
        <div className={styles.suspendedBanner}>
          <WarnIcon /> This company account is currently suspended.
        </div>
      )}

      {/* Save success */}
      {saved && (
        <div className={styles.successAlert}>
          <CheckIcon /> Company profile saved successfully.
        </div>
      )}

      {/* ── Card 1: System information (always read-only) ── */}
      <div className={styles.card}>
        <div className={styles.cardHeader}>
          <h2 className={styles.cardTitle}>System Information</h2>
        </div>
        <div className={styles.cardBody}>
          {loadSt.loading ? <LoadingSkeleton /> : loadSt.error ? (
            <div className={styles.errorAlert}><AlertIcon /> {loadSt.error}</div>
          ) : loadSt.profile && (
            <div className={styles.fieldsGrid}>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>Company Code</span>
                <span className={styles.fieldValue}>{loadSt.profile.company_code}</span>
              </div>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>Status</span>
                <CompanyStatusBadge status={loadSt.profile.status} isSuspended={loadSt.profile.is_suspended} />
              </div>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>Default Branch</span>
                <span className={loadSt.profile.default_branch_name ? styles.fieldValue : styles.fieldValueMuted}>
                  {loadSt.profile.default_branch_name ?? 'Not set'}
                </span>
              </div>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>Created</span>
                <span className={styles.fieldValue}>{fmt(loadSt.profile.created_at_utc)}</span>
              </div>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>Last Updated</span>
                <span className={loadSt.profile.updated_at_utc ? styles.fieldValue : styles.fieldValueMuted}>
                  {fmt(loadSt.profile.updated_at_utc)}
                </span>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── Card 2: Editable details ── */}
      <div className={styles.card}>
        <div className={styles.cardHeader}>
          <h2 className={styles.cardTitle}>Company Details</h2>
          {!loadSt.loading && !loadSt.error && loadSt.profile && canEdit && !editing && (
            <button className={styles.editBtn} onClick={startEdit}>Edit</button>
          )}
          {!loadSt.loading && !loadSt.error && !canEdit && (
            <span className={styles.readonlyNote}>
              <LockIcon /> Read-only
            </span>
          )}
        </div>
        <div className={styles.cardBody}>
          {loadSt.loading ? <LoadingSkeleton /> : loadSt.error ? (
            <div className={styles.errorAlert}><AlertIcon /> {loadSt.error}</div>
          ) : loadSt.profile && (
            editing ? (
              <div className={styles.form}>
                <div className={styles.formRow}>
                  <div className={styles.formGroup}>
                    <label className={styles.label} htmlFor="company_name">
                      Company Name <span className={styles.required}>*</span>
                    </label>
                    <input
                      id="company_name"
                      className={styles.input}
                      value={form.company_name}
                      onChange={(e) => setForm((f) => ({ ...f, company_name: e.target.value }))}
                      disabled={saving}
                      maxLength={120}
                      autoFocus
                    />
                  </div>
                  <div className={styles.formGroup}>
                    <label className={styles.label} htmlFor="legal_name">Legal Name</label>
                    <input
                      id="legal_name"
                      className={styles.input}
                      value={form.legal_name ?? ''}
                      onChange={(e) => setForm((f) => ({ ...f, legal_name: e.target.value || null }))}
                      disabled={saving}
                      maxLength={200}
                      placeholder="Optional"
                    />
                  </div>
                </div>

                <div className={styles.formGroup}>
                  <label className={styles.label} htmlFor="timezone_name">Timezone</label>
                  <input
                    id="timezone_name"
                    className={styles.input}
                    value={form.timezone_name ?? ''}
                    onChange={(e) => setForm((f) => ({ ...f, timezone_name: e.target.value || null }))}
                    disabled={saving}
                    placeholder="e.g. America/New_York"
                    maxLength={80}
                  />
                </div>

                <div className={styles.formGroup}>
                  <label className={styles.label} htmlFor="notes">Notes</label>
                  <textarea
                    id="notes"
                    className={styles.textarea}
                    value={form.notes ?? ''}
                    onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value || null }))}
                    disabled={saving}
                    placeholder="Optional internal notes"
                    maxLength={1000}
                  />
                </div>

                <div className={styles.toggleRow}>
                  <div className={styles.toggleInfo}>
                    <span className={styles.toggleLabel}>Allow Self-Approval</span>
                    <span className={styles.toggleDesc}>
                      When off, the user who submitted a review item cannot also approve it —
                      enforcing separation of duties in the review workflow.
                    </span>
                  </div>
                  <label className={styles.switch}>
                    <input
                      type="checkbox"
                      checked={form.allow_self_approval ?? true}
                      onChange={(e) => setForm((f) => ({ ...f, allow_self_approval: e.target.checked }))}
                      disabled={saving}
                    />
                    <span className={styles.switchTrack} />
                  </label>
                </div>

                {saveError && (
                  <div className={styles.errorAlert}><AlertIcon /> {saveError}</div>
                )}

                <div className={styles.formActions}>
                  <button className={styles.saveBtn} onClick={handleSave} disabled={saving}>
                    {saving ? <><SpinnerIcon /> Saving…</> : 'Save Changes'}
                  </button>
                  <button className={styles.cancelBtn} onClick={cancelEdit} disabled={saving}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              // Read view
              <div className={styles.fieldsGrid}>
                <div className={styles.field}>
                  <span className={styles.fieldLabel}>Company Name</span>
                  <span className={styles.fieldValue}>{loadSt.profile.company_name}</span>
                </div>
                <div className={styles.field}>
                  <span className={styles.fieldLabel}>Legal Name</span>
                  <span className={loadSt.profile.legal_name ? styles.fieldValue : styles.fieldValueMuted}>
                    {loadSt.profile.legal_name ?? '—'}
                  </span>
                </div>
                <div className={styles.field}>
                  <span className={styles.fieldLabel}>Timezone</span>
                  <span className={styles.fieldValue}>{loadSt.profile.timezone_name}</span>
                </div>
                <div className={styles.field}>
                  <span className={styles.fieldLabel}>Self-Approval</span>
                  <span className={styles.fieldValue}>
                    {loadSt.profile.allow_self_approval ? 'Allowed' : 'Disabled'}
                  </span>
                </div>
                {loadSt.profile.notes ? (
                  <div className={styles.field} style={{ gridColumn: '1 / -1' }}>
                    <span className={styles.fieldLabel}>Notes</span>
                    <span className={styles.fieldValue} style={{ fontWeight: 400, whiteSpace: 'pre-wrap' }}>
                      {loadSt.profile.notes}
                    </span>
                  </div>
                ) : null}
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function LoadingSkeleton() {
  return (
    <div className={styles.loadingWrap}>
      {[55, 35, 70, 28].map((w, i) => (
        <div key={i} className={styles.skeleton} style={{ width: `${w}%` }} />
      ))}
    </div>
  );
}

// ─── Icons ───────────────────────────────────────────────────────────────────

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      <circle cx="12" cy="12" r="10"/>
      <line x1="12" y1="8" x2="12" y2="12"/>
      <line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
  );
}

function WarnIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      <line x1="12" y1="9" x2="12" y2="13"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  );
}

function LockIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      <rect x="5" y="11" width="14" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" className={styles.spinner} aria-hidden="true">
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
    </svg>
  );
}
