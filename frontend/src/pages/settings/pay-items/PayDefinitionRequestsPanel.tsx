import { canManagePayDefinitionsForBranch } from '../../../lib/permissions';
import type { PayDefinitionDecisionAction, PayDefinitionInputType, PayDefinitionRequestEvent, PayDefinitionRequestSummary } from '../../../types/compensation';
import type { UserProfile } from '../../../store/authStore';
import { fmtDate, INPUT_TYPE_LABELS } from './payItemsModel';
import styles from './PayItemsPage.module.css';

interface PayDefinitionRequestsPanelProps {
  showRequestsTab: boolean;
  pageTab: 'items' | 'requests';
  requestFilter: PayDefinitionRequestSummary['status'] | 'all';
  setRequestFilter(value: PayDefinitionRequestSummary['status'] | 'all'): void;
  requestsLoading: boolean;
  requestsError: string;
  requests: PayDefinitionRequestSummary[];
  branchName(id: number): string;
  canGovern: boolean;
  user: UserProfile | null;
  openDecide(request: PayDefinitionRequestSummary, action: PayDefinitionDecisionAction): void;
  openDraftEdit(request: PayDefinitionRequestSummary): void;
  copyRejected(request: PayDefinitionRequestSummary): Promise<void>;
  toggleRequestHistory(request: PayDefinitionRequestSummary): Promise<void>;
  expandedRequestId: string | null;
  requestEvents: PayDefinitionRequestEvent[];
  decideTarget: PayDefinitionRequestSummary | null;
  decideAction: PayDefinitionDecisionAction | null;
  setDecideTarget(value: PayDefinitionRequestSummary | null): void;
  decideSaving: boolean;
  decideReason: string;
  setDecideReason(value: string): void;
  decideError: string;
  executeDecide(): Promise<void>;
  draftTarget: PayDefinitionRequestSummary | null;
  setDraftTarget(value: PayDefinitionRequestSummary | null): void;
  draftSaving: boolean;
  draftName: string;
  setDraftName(value: string): void;
  draftInputType: PayDefinitionInputType;
  setDraftInputType(value: PayDefinitionInputType): void;
  draftUnit: string;
  setDraftUnit(value: string): void;
  draftNotes: string;
  setDraftNotes(value: string): void;
  draftError: string;
  executeDraftEdit(): Promise<void>;
}

export function PayDefinitionRequestsPanel({
  showRequestsTab, pageTab, requestFilter, setRequestFilter, requestsLoading, requestsError,
  requests, branchName, canGovern, user, openDecide, openDraftEdit, copyRejected,
  toggleRequestHistory, expandedRequestId, requestEvents, decideTarget, decideAction,
  setDecideTarget, decideSaving, decideReason, setDecideReason, decideError, executeDecide,
  draftTarget, setDraftTarget, draftSaving, draftName, setDraftName, draftInputType,
  setDraftInputType, draftUnit, setDraftUnit, draftNotes, setDraftNotes, draftError, executeDraftEdit,
}: PayDefinitionRequestsPanelProps) {
  return (
    <>
      {showRequestsTab && pageTab === 'requests' && (
        <div className={styles.cdpiSection}>
          <div className={styles.cdpiSectionHeader}>
            <h3 className={styles.cdpiSectionTitle}>Pay Item Requests</h3>
            <div className={styles.cdpiFilterRow}>
              {(['PendingCompanyApproval', 'Draft', 'Approved', 'Rejected', 'all'] as const).map(f => (
                <button key={f} type="button"
                  className={`${styles.filterPill}${requestFilter === f ? ` ${styles.filterPillActive}` : ''}`}
                  onClick={() => setRequestFilter(f)}>
                  {f === 'all' ? 'All' : f === 'PendingCompanyApproval' ? 'Pending' : f}
                </button>
              ))}
            </div>
          </div>
          {requestsLoading && <div className={styles.cdpiEmpty}>Loading requests…</div>}
          {requestsError && <div className={styles.errorAlert}>{requestsError}</div>}
          {!requestsLoading && !requestsError && requests.length === 0 && (
            <div className={styles.cdpiEmpty}>No requests found.</div>
          )}
          <div className={styles.cdpiRequestList}>
            {requests.map(req => (
              <div key={req.request_id} className={styles.cdpiRequestCard}>
                <div className={styles.cdpiCardRow}>
                  <span className={styles.cdpiStatusBadge}>
                    {req.status === 'PendingCompanyApproval' ? 'Pending' : req.status}
                  </span>
                  <span className={styles.cdpiCardName}>{req.definition_name ?? '(unnamed)'}</span>
                  <span className={styles.cdpiCardMeta}>
                    {branchName(req.requesting_branch_id)}
                    {' · '}{req.input_type ? INPUT_TYPE_LABELS[req.input_type] : '—'}
                    {req.unit ? ` · ${req.unit}` : ''}
                  </span>
                </div>
                {req.notes && <div className={styles.cdpiCardNotes}>{req.notes}</div>}
                <div className={styles.cdpiCardDates}>
                  rev {req.revision}
                  {req.submitted_at_utc && ` · submitted ${fmtDate(req.submitted_at_utc)}`}
                  {req.copied_from_request_id && ' · copied from a rejected request'}
                </div>
                <div className={styles.cdpiCardActions}>
                  {canGovern && req.status === 'PendingCompanyApproval' && (
                    <>
                      <button type="button" className={styles.btnPrimary} onClick={() => openDecide(req, 'Approve')}>Approve</button>
                      <button type="button" className={styles.btnSecondary} onClick={() => openDecide(req, 'ReturnToDraft')}>Return to Draft</button>
                      <button type="button" className={styles.btnDanger} onClick={() => openDecide(req, 'Reject')}>Reject</button>
                    </>
                  )}
                  {req.status === 'Draft' && canManagePayDefinitionsForBranch(user, req.requesting_branch_id) && (
                    <button type="button" className={styles.btnPrimary} onClick={() => openDraftEdit(req)}>Edit and submit</button>
                  )}
                  {req.status === 'Rejected' && canManagePayDefinitionsForBranch(user, req.requesting_branch_id) && (
                    <button type="button" className={styles.btnSecondary} onClick={() => void copyRejected(req)}>Copy to new draft</button>
                  )}
                  <button type="button" className={styles.btnGhost} onClick={() => void toggleRequestHistory(req)}>
                    {expandedRequestId === req.request_id ? 'Hide history' : 'History'}
                  </button>
                </div>
                {expandedRequestId === req.request_id && (
                  <div className={styles.historySection}>
                    {requestEvents.map(event => (
                      <div key={event.event_id} className={styles.historyItem}>
                        <span>{event.event_type}</span>
                        <span className={styles.historyRange}>{fmtDate(event.occurred_at_utc)}</span>
                        {event.reason && <span className={styles.historyMeta}>{event.reason}</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {/* Decide modal */}
      {decideTarget && decideAction && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !decideSaving) setDecideTarget(null); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {decideAction === 'Approve' ? 'Approve Request' : decideAction === 'Reject' ? 'Reject Request' : 'Return to Draft'}
              </h2>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                <strong>{decideTarget.definition_name ?? '(unnamed)'}</strong> from {branchName(decideTarget.requesting_branch_id)}
              </div>
              <div className={styles.formGroup} style={{ marginTop: '0.75rem' }}>
                <label className={styles.label} htmlFor="decide-reason">Reason <span className={styles.required}>*</span></label>
                <textarea id="decide-reason" className={styles.textarea} maxLength={500}
                  value={decideReason} onChange={e => setDecideReason(e.target.value)} disabled={decideSaving} />
              </div>
              {decideError && <div className={styles.errorAlert}>{decideError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={decideAction === 'Reject' ? styles.btnDanger : styles.btnPrimary}
                disabled={decideSaving || !decideReason.trim()} onClick={() => void executeDecide()}>
                {decideSaving ? 'Working…' : decideAction === 'Approve' ? 'Approve' : decideAction === 'Reject' ? 'Reject' : 'Return to Draft'}
              </button>
              <button type="button" className={styles.btnSecondary} disabled={decideSaving} onClick={() => setDecideTarget(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Draft edit modal */}
      {draftTarget && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !draftSaving) setDraftTarget(null); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Edit Draft Request</h2></div>
            <div className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-name">Pay item name <span className={styles.required}>*</span></label>
                <input id="draft-name" className={styles.input} maxLength={200} value={draftName}
                  onChange={e => setDraftName(e.target.value)} disabled={draftSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-input">Input</label>
                <select id="draft-input" className={styles.input} value={draftInputType}
                  onChange={e => setDraftInputType(e.target.value as PayDefinitionInputType)} disabled={draftSaving}>
                  <option value="Decimal">{INPUT_TYPE_LABELS.Decimal}</option>
                  <option value="WholeNumber">{INPUT_TYPE_LABELS.WholeNumber}</option>
                </select>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-unit">Unit <span className={styles.optional}>(optional)</span></label>
                <input id="draft-unit" className={styles.input} maxLength={50} value={draftUnit}
                  onChange={e => setDraftUnit(e.target.value)} disabled={draftSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-notes">Notes <span className={styles.optional}>(optional)</span></label>
                <textarea id="draft-notes" className={styles.textarea} maxLength={500} value={draftNotes}
                  onChange={e => setDraftNotes(e.target.value)} disabled={draftSaving} />
              </div>
              {draftError && <div className={styles.errorAlert}>{draftError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={styles.btnPrimary} disabled={draftSaving || !draftName.trim()}
                onClick={() => void executeDraftEdit()}>{draftSaving ? 'Working…' : 'Save and Submit'}</button>
              <button type="button" className={styles.btnSecondary} disabled={draftSaving} onClick={() => setDraftTarget(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}