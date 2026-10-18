import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import type { BranchPolicySummaryResponse, SetupResponse } from '../../../types/payrollSetup';
import type { PolicyUsageState } from './policyMasterView';

export type ConfirmKind = 'clear-default' | 'set-default' | 'archive';

export interface ActionError {
  kind: 'default' | 'archive';
  /** The policy this error belongs to; null for clear-default (not tied to any policy). */
  setupId: number | null;
  info: ApiErrorInfo;
}

export interface EditForm {
  setup_name: string;
  description: string;
}

export interface DraftActionError {
  kind: 'discard';
  setupId: number;
  draftId: number;
  info: ApiErrorInfo;
}

export type PolicyDetailPanelProps = {
  selectedPolicy: SetupResponse | null;
  /** Derived usage state (policyMasterView.policyUsage) — never the raw setup.status. */
  usageState: PolicyUsageState;
  defaultSetupId: number | null;
  summaries: readonly BranchPolicySummaryResponse[];
  summariesLoading: boolean;
  summariesError: ApiErrorInfo | null;
  canManage: boolean;
  canAssign: boolean;
  canPublish: boolean;
  mutating: boolean;
  setMutating: (value: boolean) => void;
  /** Refreshes the shell's setups list + company default (e.g. after archive/edit/default changes). */
  onReloadSetups: () => Promise<void>;
  /** Switches the shell to the Branch Assignments tab (the "Assign branches" next step). */
  onGoToAssignments: () => void;
  showToast: (message: string) => void;
  /** Mounts the not-yet-designed lower sections. Off until their steps land. */
  showLowerSections?: boolean;
};

export type DraftEditorMode = 'create' | 'edit';
