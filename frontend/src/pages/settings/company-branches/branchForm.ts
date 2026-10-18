import type { BranchAdmin } from '../../../types/settings';

export const BRANCH_STATUSES = ['Active', 'Inactive', 'Closed'] as const;
export type BranchStatus = typeof BRANCH_STATUSES[number];

export const COUNTRIES = [
  '', 'Egypt', 'United Arab Emirates', 'Saudi Arabia', 'Kuwait', 'Qatar',
  'Bahrain', 'Oman', 'Jordan', 'Lebanon', 'Iraq', 'Libya', 'Morocco', 'Tunisia',
  'United States', 'United Kingdom', 'Canada', 'Australia',
  'Germany', 'France', 'Netherlands', 'Turkey', 'Other',
];

export interface BranchForm {
  branch_name: string; branch_code: string; status: BranchStatus;
  time_zone_id: string;
  is_default: boolean; address_line1: string; city: string;
  state_province: string; postal_code: string; country: string; notes: string;
}

export const EMPTY_FORM: BranchForm = {
  branch_name: '', branch_code: '', status: 'Active', time_zone_id: '', is_default: false,
  address_line1: '', city: '', state_province: '', postal_code: '', country: '', notes: '',
};

export function toForm(b: BranchAdmin): BranchForm {
  return {
    branch_name: b.branch_name, branch_code: b.branch_code, status: b.status as BranchStatus,
    time_zone_id: b.time_zone_id,
    is_default: b.is_default, address_line1: b.address_line1 ?? '',
    city: b.city ?? '', state_province: b.state_province ?? '',
    postal_code: b.postal_code ?? '', country: b.country ?? '', notes: b.notes ?? '',
  };
}
