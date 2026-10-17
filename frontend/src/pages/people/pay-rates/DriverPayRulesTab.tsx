import type { Dispatch, SetStateAction } from 'react';
import { formatMoney } from '../../../lib/money';
import type { DriverPayRule } from './driverPayRulesApi';
import { today, type RuleForm } from './payRatesView';
import styles from './PayRatesPage.module.css';

type DriverPayRulesTabProps = {
  payRules: DriverPayRule[];
  payRulesError: string;
  loadingPayRules: boolean;
  canEditMatrix: boolean;
  currencyCode: string | null | undefined;
  currencyDigits: number | null | undefined;
  ruleForm: RuleForm | null;
  setRuleForm: Dispatch<SetStateAction<RuleForm | null>>;
  savingRule: boolean;
  ruleFormError: string;
  setRuleFormError: (error: string) => void;
  voidPayRule: (ruleId: number) => Promise<void>;
  createPayRule: () => Promise<void>;
  endPayRule: (ruleId: number) => Promise<void>;
};

export function DriverPayRulesTab({ payRules, payRulesError, loadingPayRules, canEditMatrix, currencyCode, currencyDigits, ruleForm, setRuleForm, savingRule, ruleFormError, setRuleFormError, voidPayRule, createPayRule, endPayRule }: DriverPayRulesTabProps) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead}>
        <span className={styles.sectionTitle}>Pay Rules</span>
        <span className={styles.sectionNote}>Minimum &amp; Maximum Pay per period</span>
      </div>
      <div className={styles.payRulesInfo}>
        Pay Rules set a floor (Minimum Pay) or ceiling (Maximum Pay) applied
        during finalization. They are separate from the rate matrix.
      </div>
      {payRulesError && <div className={styles.errBanner} style={{ margin: '0 1.25rem 0.5rem' }}>{payRulesError}</div>}
      {loadingPayRules ? <div className={styles.loading}>Loading pay rules...</div> : (
        <>
          {(['MinimumPay', 'MaximumPay'] as const).map((ruleType) => {
            const active = payRules.find(r => r.rule_type === ruleType && r.status === 'Active');
            const ended  = payRules.filter(r => r.rule_type === ruleType && r.status === 'Ended');
            const voided = payRules.filter(r => r.rule_type === ruleType && r.status === 'Voided');
            const label  = ruleType === 'MinimumPay' ? 'Minimum Pay' : 'Maximum Pay';
            const isFormOpenForThis = ruleForm?.ruleType === ruleType;
            const isAddForm = isFormOpenForThis && ruleForm?.action === 'add';
            const isEndForm = isFormOpenForThis && ruleForm?.action === 'end';

            return (
              <div key={ruleType} className={styles.payRuleCard}>
                <div className={styles.payRuleHeader}>
                  <strong>{label}</strong>
                  {active ? <span className={styles.badgeApproved}>Active</span> : <span className={styles.badgeVoided}>Not set</span>}
                  {canEditMatrix && !isFormOpenForThis && (
                    <div className={styles.payRuleActions}>
                      {!active && <button className={styles.btnApprove} onClick={() => { setRuleForm({ ruleType, action: 'add', amount: '', effectiveFrom: today(), effectiveTo: '', notes: '' }); setRuleFormError(''); }}>+ Add</button>}
                      {active && <>
                        <button className={styles.btnSecondary} style={{ fontSize: '0.75rem', padding: '0.2rem 0.55rem' }} onClick={() => { setRuleForm({ ruleType, action: 'end', amount: '', effectiveFrom: '', effectiveTo: today(), notes: '' }); setRuleFormError(''); }}>End</button>
                        <button className={styles.btnVoid} style={{ fontSize: '0.75rem', padding: '0.2rem 0.55rem' }} disabled={savingRule} onClick={() => void voidPayRule(active.driver_pay_rule_id)}>Void</button>
                      </>}
                    </div>
                  )}
                </div>
                {active && !isEndForm && <div className={styles.payRuleRow}>
                  <span className={styles.payRuleAmount}>{formatMoney(active.amount, currencyCode, currencyDigits)}<span className={styles.payRuleUnit}>/period</span></span>
                  <span className={styles.payRuleMeta}>From {active.effective_from}{active.effective_to ? ` to ${active.effective_to}` : ' (open-ended)'}</span>
                </div>}
                {active?.notes && !isEndForm && <div className={styles.payRuleNotes}>{active.notes}</div>}
                {isAddForm && <div className={styles.payRuleForm}>
                  <div className={styles.payRuleFormRow}>
                    <div className={styles.payRuleFormGroup}>
                      <label className={styles.formLabel}>Amount per period</label>
                      <input className={styles.editInput} type="number" step="0.0001" min="0.0001" placeholder="e.g. 150.0000" value={ruleForm.amount} onChange={e => setRuleForm(f => f ? { ...f, amount: e.target.value } : f)} />
                    </div>
                    <div className={styles.payRuleFormGroup}>
                      <label className={styles.formLabel}>Effective from</label>
                      <input className={styles.editDateInput} type="date" value={ruleForm.effectiveFrom} onChange={e => setRuleForm(f => f ? { ...f, effectiveFrom: e.target.value } : f)} />
                    </div>
                  </div>
                  <div className={styles.payRuleFormGroup}>
                    <label className={styles.formLabel}>Notes (optional)</label>
                    <input className={styles.editInput} style={{ width: '100%' }} type="text" placeholder="Optional note" value={ruleForm.notes} onChange={e => setRuleForm(f => f ? { ...f, notes: e.target.value } : f)} />
                  </div>
                  {ruleFormError && <div className={styles.actionErr} style={{ borderRadius: 6, margin: '0.25rem 0' }}>{ruleFormError}</div>}
                  <div className={styles.payRuleFormButtons}>
                    <button className={styles.btnPrimary} disabled={savingRule || !ruleForm.amount || !ruleForm.effectiveFrom} onClick={() => void createPayRule()}>{savingRule ? 'Saving…' : `Add ${label}`}</button>
                    <button className={styles.btnSecondary} disabled={savingRule} onClick={() => { setRuleForm(null); setRuleFormError(''); }}>Cancel</button>
                  </div>
                </div>}
                {isEndForm && active && <div className={styles.payRuleForm}>
                  <p className={styles.payRuleFormDesc}>
                    Ending this rule will close it at the date you specify.
                    The current amount is <strong>{formatMoney(active.amount, currencyCode, currencyDigits)}</strong>.
                    Historical finalized periods that relied on this rule will not be affected.
                  </p>
                  <div className={styles.payRuleFormRow}>
                    <div className={styles.payRuleFormGroup}>
                      <label className={styles.formLabel}>End date (effective to)</label>
                      <input className={styles.editDateInput} type="date" value={ruleForm.effectiveTo} onChange={e => setRuleForm(f => f ? { ...f, effectiveTo: e.target.value } : f)} />
                    </div>
                  </div>
                  {ruleFormError && <div className={styles.actionErr} style={{ borderRadius: 6, margin: '0.25rem 0' }}>{ruleFormError}</div>}
                  <div className={styles.payRuleFormButtons}>
                    <button className={styles.btnPrimary} disabled={savingRule || !ruleForm.effectiveTo} onClick={() => void endPayRule(active.driver_pay_rule_id)}>{savingRule ? 'Saving…' : 'End Rule'}</button>
                    <button className={styles.btnSecondary} disabled={savingRule} onClick={() => { setRuleForm(null); setRuleFormError(''); }}>Cancel</button>
                  </div>
                </div>}
                {!active && !isAddForm && payRules.filter(r => r.rule_type === ruleType).length === 0 && <div className={styles.emptySection} style={{ padding: '0.5rem 0' }}>No {label.toLowerCase()} rule set.</div>}
                {(ended.length > 0 || voided.length > 0) && <details className={styles.payRuleHistory}>
                  <summary style={{ cursor: 'pointer', fontSize: '0.78rem', color: '#6b7280', padding: '0.3rem 0' }}>{ended.length + voided.length} historical record{ended.length + voided.length !== 1 ? 's' : ''}</summary>
                  <table className={styles.rateTable} style={{ marginTop: '0.5rem' }}>
                    <thead><tr><th>Amount</th><th>Period</th><th>Status</th></tr></thead>
                    <tbody>{[...ended, ...voided].map(r => (
                      <tr key={r.driver_pay_rule_id}>
                        <td>{formatMoney(r.amount, currencyCode, currencyDigits)}</td>
                        <td>{r.effective_from}{r.effective_to ? ` – ${r.effective_to}` : ''}</td>
                        <td><span className={r.status === 'Ended' ? styles.badgeSuperseded : styles.badgeVoided}>{r.status}</span></td>
                      </tr>
                    ))}</tbody>
                  </table>
                </details>}
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
