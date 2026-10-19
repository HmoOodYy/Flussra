import apiClient from '../../../lib/apiClient';
import type { AxiosPromise } from 'axios';

/**
 * A Min/Max rule owned by the permanent Driver. The calendar fields are the home pay calendar
 * the server captured from the Payroll Setup of the Branch of the rule's context tenure; they
 * are read-only. calendar_reconciliation_needed_from is a read-only hint (see payRulesView).
 */
export interface DriverPayRule {
  driver_pay_rule_id: number;
  driver_id: number;
  rule_type: string;
  amount: string;
  effective_from: string;
  effective_to: string | null;
  status: string;
  calendar_frequency: string;
  calendar_anchor_start_date: string;
  calendar_custom_interval_days: number | null;
  calendar_source_branch_id: number;
  calendar_source_payroll_setup_version_id: number;
  calendar_reconciliation_needed_from: string | null;
  notes: string | null;
  created_at_utc: string;
}

/** No calendar field: the server records the calendar. */
export interface DriverPayRuleCreatePayload {
  driver_id: number;
  rule_type: 'MinimumPay' | 'MaximumPay';
  amount: string;
  effective_from: string;
  notes: string | null;
}

export interface DriverPayRuleEndPayload {
  effective_to: string;
}

export function getDriverPayRules(driverId: number): AxiosPromise<DriverPayRule[]> {
  return apiClient.get<DriverPayRule[]>(`/payroll/drivers/${driverId}/pay-rules`);
}

export function createDriverPayRule(payload: DriverPayRuleCreatePayload): AxiosPromise<DriverPayRule> {
  return apiClient.post<DriverPayRule>('/payroll/driver-pay-rules', payload);
}

export function endDriverPayRule(ruleId: number, payload: DriverPayRuleEndPayload): AxiosPromise<DriverPayRule> {
  return apiClient.post<DriverPayRule>(`/payroll/driver-pay-rules/${ruleId}/end`, payload);
}

export function voidDriverPayRule(ruleId: number): AxiosPromise<DriverPayRule> {
  return apiClient.post<DriverPayRule>(`/payroll/driver-pay-rules/${ruleId}/void`);
}
