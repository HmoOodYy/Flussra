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
}
type VersionsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; versions: VersionResponse[] }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo }
  | { type: 'CLEAR' };

function versionsReducer(s: VersionsState, a: VersionsAction): VersionsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: null };
    case 'FETCH_OK': return { versions: a.versions, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'CLEAR': return { versions: [], loading: false, error: null };
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

export function usePolicyDetailData(selectedPolicyId: number | null) {
  const [setupDataReloadKey, setSetupDataReloadKey] = useState(0);

  const [versionsState, dispatchVersions] = useReducer(versionsReducer, {
    versions: [],
    loading: false,
    error: null,
  });
  const { versions, loading: versionsLoading, error: versionsError } = versionsState;

  useEffect(() => {
    if (selectedPolicyId == null) {
      dispatchVersions({ type: 'CLEAR' });
      return;
    }
    let active = true;
    dispatchVersions({ type: 'FETCH_START' });
    listPayrollSetupVersions(selectedPolicyId)
      .then((v) => {
        if (active) dispatchVersions({ type: 'FETCH_OK', versions: v });
      })
      .catch((err) => {
        if (active) dispatchVersions({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load policy updates.') });
      });
    return () => {
      active = false;
    };
  }, [selectedPolicyId, setupDataReloadKey]);

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
