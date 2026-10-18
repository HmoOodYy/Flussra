import { useEffect, useReducer, useState } from 'react';
import { listPayrollSetupDrafts, listPayrollSetupVersions } from '../../../lib/payrollSetupApi';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import type { DraftResponse, VersionResponse } from '../../../types/payrollSetup';

// ── Async data-loading reducers (Policy updates / Drafts of the selected policy) ─

interface VersionsState {
  versions: VersionResponse[];
  loading: boolean;
  error: ApiErrorInfo | null;
  setupId: number | null;
  branchId: number | null;
}
type VersionsAction =
  | { type: 'FETCH_START'; setupId: number; branchId: number | null }
  | { type: 'FETCH_OK'; versions: VersionResponse[] }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo }
  | { type: 'CLEAR' };

function versionsReducer(s: VersionsState, a: VersionsAction): VersionsState {
  switch (a.type) {
    case 'FETCH_START': return { versions: [], loading: true, error: null, setupId: a.setupId, branchId: a.branchId };
    case 'FETCH_OK': return { ...s, versions: a.versions, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'CLEAR': return { versions: [], loading: false, error: null, setupId: null, branchId: null };
  }
}

interface DraftsState {
  drafts: DraftResponse[];
  loading: boolean;
  error: ApiErrorInfo | null;
}
type DraftsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; drafts: DraftResponse[] }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo }
  | { type: 'CLEAR' };

function draftsReducer(s: DraftsState, a: DraftsAction): DraftsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: null };
    case 'FETCH_OK': return { drafts: a.drafts, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'CLEAR': return { drafts: [], loading: false, error: null };
  }
}

export function usePolicyDetailData(selectedPolicyId: number | null, branchId: number | null) {
  const [setupDataReloadKey, setSetupDataReloadKey] = useState(0);

  const [versionsState, dispatchVersions] = useReducer(versionsReducer, {
    versions: [],
    loading: false,
    error: null,
    setupId: null,
    branchId: null,
  });
  const hasVersionContext = versionsState.setupId === selectedPolicyId && versionsState.branchId === branchId;
  const versions = hasVersionContext ? versionsState.versions : [];
  const versionsLoading = selectedPolicyId != null && (!hasVersionContext || versionsState.loading);
  const versionsError = hasVersionContext ? versionsState.error : null;

  useEffect(() => {
    if (selectedPolicyId == null) {
      dispatchVersions({ type: 'CLEAR' });
      return;
    }
    let active = true;
    dispatchVersions({ type: 'FETCH_START', setupId: selectedPolicyId, branchId });
    listPayrollSetupVersions(selectedPolicyId, branchId)
      .then((v) => {
        if (active) dispatchVersions({ type: 'FETCH_OK', versions: v });
      })
      .catch((err) => {
        if (active) dispatchVersions({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load policy updates.') });
      });
    return () => {
      active = false;
    };
  }, [selectedPolicyId, branchId, setupDataReloadKey]);

  const [draftsState, dispatchDrafts] = useReducer(draftsReducer, {
    drafts: [],
    loading: false,
    error: null,
  });
  const { drafts, loading: draftsLoading, error: draftsError } = draftsState;

  useEffect(() => {
    if (selectedPolicyId == null) {
      dispatchDrafts({ type: 'CLEAR' });
      return;
    }
    let active = true;
    dispatchDrafts({ type: 'FETCH_START' });
    listPayrollSetupDrafts(selectedPolicyId)
      .then((d) => {
        if (active) dispatchDrafts({ type: 'FETCH_OK', drafts: d });
      })
      .catch((err) => {
        if (active) dispatchDrafts({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load draft schedules.') });
      });
    return () => {
      active = false;
    };
  }, [selectedPolicyId, setupDataReloadKey]);

  return { versions, versionsLoading, versionsError, drafts, draftsLoading, draftsError, setSetupDataReloadKey };
}
