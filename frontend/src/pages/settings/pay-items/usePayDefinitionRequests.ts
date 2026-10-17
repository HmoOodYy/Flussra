import { useCallback, useEffect, useState } from 'react';
import {
  copyPayDefinitionRequest,
  decidePayDefinitionRequest,
  listPayDefinitionRequestEvents,
  listPayDefinitionRequests,
  submitPayDefinitionRequest,
  updatePayDefinitionRequest,
} from '../../../lib/compensationApi';
import type {
  PayDefinitionDecisionAction,
  PayDefinitionInputType,
  PayDefinitionRequestEvent,
  PayDefinitionRequestSummary,
} from '../../../types/compensation';
import { saveAndSubmitDraft } from './requestDraftWorkflow';
import { apiError, type RequestFilter } from './payItemsModel';

interface UsePayDefinitionRequestsArgs {
  selectedBranchId: number | null;
  canGovern: boolean;
  showRequestsTab: boolean;
  pageTab: 'items' | 'requests';
  setReloadKey(value: number | ((current: number) => number)): void;
  showToast(message: string): void;
}

export function usePayDefinitionRequests({
  selectedBranchId, canGovern, showRequestsTab, pageTab, setReloadKey, showToast,
}: UsePayDefinitionRequestsArgs) {
  const [requests, setRequests] = useState<PayDefinitionRequestSummary[]>([]);
  const [requestsLoading, setRequestsLoading] = useState(false);
  const [requestsError, setRequestsError] = useState('');
  const [requestFilter, setRequestFilter] = useState<RequestFilter>('PendingCompanyApproval');
  const [requestKey, setRequestKey] = useState(0);
  const [expandedRequestId, setExpandedRequestId] = useState<string | null>(null);
  const [requestEvents, setRequestEvents] = useState<PayDefinitionRequestEvent[]>([]);
  const [decideTarget, setDecideTarget] = useState<PayDefinitionRequestSummary | null>(null);
  const [decideAction, setDecideAction] = useState<PayDefinitionDecisionAction | null>(null);
  const [decideReason, setDecideReason] = useState('');
  const [decideSaving, setDecideSaving] = useState(false);
  const [decideError, setDecideError] = useState('');
  const [draftTarget, setDraftTarget] = useState<PayDefinitionRequestSummary | null>(null);
  const [draftName, setDraftName] = useState('');
  const [draftInputType, setDraftInputType] = useState<PayDefinitionInputType>('Decimal');
  const [draftUnit, setDraftUnit] = useState('');
  const [draftNotes, setDraftNotes] = useState('');
  const [draftSaving, setDraftSaving] = useState(false);
  const [draftError, setDraftError] = useState('');

  const loadRequests = useCallback(async () => {
    setRequestsLoading(true);
    setRequestsError('');
    try {
      setRequests(await listPayDefinitionRequests({
        ...(requestFilter !== 'all' ? { status: requestFilter } : {}),
        ...(!canGovern && selectedBranchId ? { branch_id: selectedBranchId } : {}),
      }));
    } catch (e) {
      setRequestsError(apiError(e));
    } finally {
      setRequestsLoading(false);
    }
  }, [requestFilter, canGovern, selectedBranchId]);

  useEffect(() => {
    if (!showRequestsTab || pageTab !== 'requests') return;
    void (async () => { await loadRequests(); })();
  }, [pageTab, requestKey, showRequestsTab, loadRequests]);

  function openDecide(request: PayDefinitionRequestSummary, action: PayDefinitionDecisionAction) {
    setDecideTarget(request);
    setDecideAction(action);
    setDecideReason('');
    setDecideError('');
  }

  async function executeDecide() {
    if (!decideTarget || !decideAction) return;
    if (!decideReason.trim()) {
      setDecideError('A reason is required before proceeding.');
      return;
    }
    setDecideSaving(true);
    setDecideError('');
    try {
      await decidePayDefinitionRequest(decideTarget.request_id, {
        action: decideAction, expected_revision: decideTarget.revision, reason: decideReason.trim(),
      });
      showToast(decideAction === 'Approve' ? 'Request approved.'
        : decideAction === 'Reject' ? 'Request rejected.' : 'Request returned to draft.');
      setDecideTarget(null);
      setRequestKey(k => k + 1);
      if (decideAction === 'Approve') setReloadKey(k => k + 1);
    } catch (e) {
      setDecideError(apiError(e));
    } finally {
      setDecideSaving(false);
    }
  }

  function openDraftEdit(request: PayDefinitionRequestSummary) {
    setDraftTarget(request);
    setDraftName(request.definition_name ?? '');
    setDraftInputType(request.input_type ?? 'Decimal');
    setDraftUnit(request.unit ?? '');
    setDraftNotes(request.notes ?? '');
    setDraftError('');
  }

  async function executeDraftEdit() {
    if (!draftTarget) return;
    if (!draftName.trim()) {
      setDraftError('Pay item name is required.');
      return;
    }
    setDraftSaving(true);
    setDraftError('');
    try {
      const result = await saveAndSubmitDraft(
        { updatePayDefinitionRequest, submitPayDefinitionRequest }, draftTarget,
        { definition_name: draftName.trim(), input_type: draftInputType,
          unit: draftUnit.trim(), notes: draftNotes.trim() });
      setDraftTarget(null);
      setRequestKey(k => k + 1);
      showToast(result.kind === 'submitted'
        ? 'Request submitted for company approval.'
        : `Draft saved but could not be submitted: ${apiError(result.error)}`);
    } catch (e) {
      setDraftError(apiError(e));
    } finally {
      setDraftSaving(false);
    }
  }

  async function copyRejected(request: PayDefinitionRequestSummary) {
    try {
      await copyPayDefinitionRequest(request.request_id);
      setRequestFilter('Draft');
      setRequestKey(k => k + 1);
      showToast('A new draft was created from the rejected request.');
    } catch (e) {
      showToast(apiError(e));
    }
  }

  async function toggleRequestHistory(request: PayDefinitionRequestSummary) {
    if (expandedRequestId === request.request_id) {
      setExpandedRequestId(null);
      return;
    }
    setExpandedRequestId(request.request_id);
    try {
      setRequestEvents(await listPayDefinitionRequestEvents(request.request_id));
    } catch {
      setRequestEvents([]);
    }
  }

  return {
    requests, requestsLoading, requestsError, requestFilter, setRequestFilter,
    setRequestKey, expandedRequestId, requestEvents,
    decideTarget, setDecideTarget, decideAction, decideReason, setDecideReason,
    decideSaving, decideError, openDecide, executeDecide,
    draftTarget, setDraftTarget, draftName, setDraftName, draftInputType, setDraftInputType,
    draftUnit, setDraftUnit, draftNotes, setDraftNotes, draftSaving, draftError,
    openDraftEdit, executeDraftEdit, copyRejected, toggleRequestHistory,
  };
}