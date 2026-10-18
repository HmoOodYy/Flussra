import apiClient from '../../../lib/apiClient';
import type { AxiosPromise } from 'axios';

export interface DriverPayRule {
  driver_pay_rule_id: number;
  driver_assignment_id: number;
  rule_type: string;
  amount: string;
  effective_from: string;
  effective_to: string | null;
  status: string;
  notes: string | null;
  created_at_utc: string;
}

export interface DriverPayRuleCreatePayload {
  driver_assignment_id: number;
  rule_type: 'MinimumPay' | 'MaximumPay';
  amount: string;
  effective_from: string;
  notes: string | null;
}

export interface DriverPayRuleEndPayload {
  effective_to: string;
}

export function getDriverPayRules(driverAssignmentId: number): AxiosPromise<DriverPayRule[]> {
  return apiClient.get<DriverPayRule[]>(`/payroll/driver-assignments/${driverAssignmentId}/pay-rules`);
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
