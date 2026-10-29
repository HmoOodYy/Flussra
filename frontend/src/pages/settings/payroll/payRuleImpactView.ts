/**
 * Pure view model for the "pay rules would stop fitting" warning shown beside the publish and
 * change-policy previews.
 *
 * PURE MODULE: no React, no apiClient. Which rules are affected is decided only by the backend
 * (`affected_pay_rules` on the impact previews); this module only words and orders what it
 * returns. The warning never blocks the change.
 */
import { frequencyNoun } from '../../../lib/payrollBoundaryView.ts';
import type { AffectedPayRuleResponse } from '../../../types/payrollSetup.ts';

export const MAX_VISIBLE_PAY_RULES = 8;

export interface PayRuleImpactRow {
  key: string;
  driver: string;
  branch: string;
  ruleType: string;
  calendar: string;
}

export interface PayRuleImpactView {
  driverCount: number;
  message: string;
  rows: PayRuleImpactRow[];
  hiddenCount: number;
}

export function payRuleTypeLabel(ruleType: string): string {
  switch (ruleType) {
    case 'MinimumPay':
      return 'Minimum pay';
    case 'MaximumPay':
      return 'Maximum pay';
    default:
      return ruleType;
  }
}

function driverLabel(rule: AffectedPayRuleResponse): string {
  return rule.driver_number ? `${rule.driver_name} (${rule.driver_number})` : rule.driver_name;
}

function calendarLabel(rule: AffectedPayRuleResponse): string {
  return `${frequencyNoun(rule.calendar_frequency, rule.calendar_custom_interval_days).toLowerCase()} calendar`;
}

/** The warning text and compact list, or null when no rule is affected. */
export function buildPayRuleImpactView(
  rules: readonly AffectedPayRuleResponse[] | null | undefined,
): PayRuleImpactView | null {
  if (!rules || rules.length === 0) return null;
  const driverCount = new Set(rules.map((rule) => rule.driver_id)).size;
  const subject =
    driverCount === 1
      ? '1 driver has minimum or maximum pay set for the old calendar.'
      : `${driverCount} drivers have minimum or maximum pay set for the old calendar.`;
  const message =
    `${subject} Their payroll can't be submitted until those rules are ended and set again ` +
    'for the new calendar.';
  const rows = rules.slice(0, MAX_VISIBLE_PAY_RULES).map((rule) => ({
    key: `${rule.rule_id}:${rule.branch_id}`,
    driver: driverLabel(rule),
    branch: rule.branch_name,
    ruleType: payRuleTypeLabel(rule.rule_type),
    calendar: calendarLabel(rule),
  }));
  return { driverCount, message, rows, hiddenCount: Math.max(0, rules.length - rows.length) };
}
