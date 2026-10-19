import { useState } from 'react';
import { apiError, type RuleForm } from './payRatesView';
import { createDriverPayRule, endDriverPayRule, voidDriverPayRule } from './driverPayRulesApi';

export function useDriverPayRules(
  selectedDriverAssignmentId: number | null,
  loadPayRules: (driverAssignmentId: number) => Promise<void>,
  setPayRulesError: (error: string) => void,
) {
  const [ruleForm, setRuleForm] = useState<RuleForm | null>(null);
  const [savingRule, setSavingRule] = useState(false);
  const [ruleFormError, setRuleFormError] = useState('');

  async function voidPayRule(ruleId: number) {
    if (!selectedDriverAssignmentId) return;
    setSavingRule(true);
    setRuleFormError('');
    try {
      await voidDriverPayRule(ruleId);
      await loadPayRules(selectedDriverAssignmentId);
    } catch (err) {
      setPayRulesError(apiError(err));
    } finally {
      setSavingRule(false);
    }
  }

  async function createPayRule() {
    if (!selectedDriverAssignmentId || !ruleForm || ruleForm.action !== 'add') return;
    setSavingRule(true);
    setRuleFormError('');
    try {
      await createDriverPayRule({
        driver_assignment_id: selectedDriverAssignmentId,
        rule_type: ruleForm.ruleType,
        amount: ruleForm.amount,
        effective_from: ruleForm.effectiveFrom,
        notes: ruleForm.notes || null,
      });
      setRuleForm(null);
      await loadPayRules(selectedDriverAssignmentId);
    } catch (err) {
      setRuleFormError(apiError(err));
    } finally {
      setSavingRule(false);
    }
  }

  async function endPayRule(ruleId: number) {
    if (!selectedDriverAssignmentId || !ruleForm || ruleForm.action !== 'end') return;
    setSavingRule(true);
    setRuleFormError('');
    try {
      await endDriverPayRule(ruleId, {
        effective_to: ruleForm.effectiveTo,
      });
      setRuleForm(null);
      await loadPayRules(selectedDriverAssignmentId);
    } catch (err) {
      setRuleFormError(apiError(err));
    } finally {
      setSavingRule(false);
    }
  }

  return { ruleForm, setRuleForm, savingRule, ruleFormError, setRuleFormError, voidPayRule, createPayRule, endPayRule };
}
