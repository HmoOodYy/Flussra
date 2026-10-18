import type { Dispatch, SetStateAction } from 'react';
import { ErrorState } from '../../../components/ui';
import { CompanyStatusBadge } from '../../../components/StatusBadge';
import { AlertIcon, EditIcon, SkeletonBlock, SpinnerIcon } from './CompanyBranchesIcons';
import { fmtDate } from './companyBranchesPresentation';
import type { CompanyProfile, CompanyUpdate, SupportedCurrency } from '../../../types/settings';
import styles from './CompanyBranchesPage.module.css';

export function CompanyProfileCard({
  company, companyLoading, companyError, canAdmin, startEditCo,
}: {
  company: CompanyProfile | null; companyLoading: boolean; companyError: string;
  canAdmin: boolean; startEditCo: () => void;
}) {
  return (
    <>
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
              {canAdmin && (
                <button className={styles.btnSecondary} onClick={startEditCo}>
                  <EditIcon /> Edit Company
                </button>
              )}
            </div>
          </div>

          <div className={styles.companyMeta}>
            <MetaItem label="Company Code"   value={company.company_code} />
            <MetaItem label="Default Branch"  value={company.default_branch_name ?? '—'} />
            <MetaItem label="Self-Approval"   value={company.allow_self_approval ? 'Allowed' : 'Disabled'} />
            <MetaItem label="Company Currency" value={company.currency_code ? `${company.currency_code}${company.currency_name ? ` · ${company.currency_name}` : ''}` : 'Required before payroll money can be entered'} wide />
            <MetaItem label="Created"         value={fmtDate(company.created_at_utc)} />
            <MetaItem label="Last Updated"    value={fmtDate(company.updated_at_utc)} />
            {company.notes && (
              <MetaItem label="Notes" value={company.notes} wide />
            )}
          </div>
        </div>
      )}
    </>
  );
}

export function CompanyEditDialog({
  editingCompany, company, setEditingCompany, savingCo, coForm, setCoForm,
  currencies, currencyLoadError, coSaveErr, requestSaveCo,
}: {
  editingCompany: boolean; company: CompanyProfile | null;
  setEditingCompany: Dispatch<SetStateAction<boolean>>; savingCo: boolean;
  coForm: CompanyUpdate; setCoForm: Dispatch<SetStateAction<CompanyUpdate>>;
  currencies: SupportedCurrency[]; currencyLoadError: string; coSaveErr: string;
  requestSaveCo: () => void;
}) {
  return (
    <>
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
                <div className={styles.formGroupFull}>
                  <label className={styles.label}>Company Currency <span className={styles.required}>*</span></label>
                  {company.currency_change_locked ? (
                    <>
                      <input className={styles.input} value={company.currency_code ?? ''} disabled />
                      <p className={styles.fieldHelp}>Currency is locked because this company has recorded monetary payroll history.</p>
                    </>
                  ) : (
                    <>
                      <select className={styles.input}
                        value={coForm.currency_code ?? company.currency_code ?? ''}
                        onChange={(e) => setCoForm((f) => ({ ...f, currency_code: e.target.value }))}
                        disabled={savingCo || currencies.length === 0}>
                        <option value="">{currencies.length ? 'Select a currency' : 'Loading currencies…'}</option>
                        {currencies.map((currency) => <option key={currency.currency_code} value={currency.currency_code}>
                          {currency.currency_code} — {currency.currency_name}
                        </option>)}
                      </select>
                      {currencyLoadError && <p className={styles.fieldHelp}>{currencyLoadError}</p>}
                      {!company.currency_code && <p className={styles.fieldHelp}>Choose the company currency before entering payroll amounts.</p>}
                    </>
                  )}
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
    </>
  );
}

function MetaItem({ label, value, wide }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={styles.metaItem} style={wide ? { gridColumn: '1 / -1' } : undefined}>
      <span className={styles.metaLabel}>{label}</span>
      <span className={styles.metaValue}>{value}</span>
    </div>
  );
}
