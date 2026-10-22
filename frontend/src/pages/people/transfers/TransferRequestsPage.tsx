import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import type { Branch } from '../../../types/core';
import { apiError } from '../peoplePresentation';
import { useTransferRequests } from './useTransferRequests';
import { TransferRequestList } from './TransferRequestList';
import { TransferDetail } from './TransferDetail';
import { TransferRequestDialogs } from './TransferRequestDialogs';
import styles from '../PeoplePage.module.css';
import trStyles from './TransferRequests.module.css';

/**
 * Transfer Requests: the existing transfer lifecycle, routed on its own under the People hub with
 * its own permission boundary. It reads only transfer and Branch data, so a transfer-only operator
 * needs no Workforce or Access permission.
 */
export function TransferRequestsPage() {
  const [branches, setBranches] = useState<Branch[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  // A Driver's "Request Transfer" link lands here once; the parameter is consumed so a reload
  // does not reopen the form.
  const [newFor] = useState(() => Number(searchParams.get('newFor')) || null);
  useEffect(() => {
    if (newFor === null) return;
    const next = new URLSearchParams(window.location.search);
    next.delete('newFor');
    setSearchParams(next, { replace: true });
  }, [newFor, setSearchParams]);

  useEffect(() => {
    let cancelled = false;
    apiClient.get<Branch[]>('/core/branches')
      .then((r) => { if (!cancelled) setBranches(r.data); })
      .catch((e) => { if (!cancelled) setError(apiError(e)); });
    return () => { cancelled = true; };
  }, []);

  if (error) return <div className={styles.splash} style={{ color: '#dc2626' }}>{error}</div>;
  if (branches === null) return <div className={styles.splash}>Loading…</div>;
  return <TransferRequests branches={branches} newForDriverId={newFor} />;
}

function TransferRequests({ branches, newForDriverId }: { branches: Branch[]; newForDriverId: number | null }) {
  const {
    st, dispatch, canEditAnyBranch, filtered, selected,
    canApproveSource, canDecideTarget, canActOnEitherBranch,
    openCreate, submitCreate, submitApproveSource, submitDecide, doComplete, submitCancel,
    submitReschedule,
  } = useTransferRequests(branches, newForDriverId);

  if (st.loading) return <div className={styles.splash}>Loading transfer requests…</div>;
  if (st.loadErr) return <div className={styles.splash} style={{ color: '#dc2626' }}>Failed to load: {st.loadErr}</div>;

  return (
    <div className={styles.body}>
      <TransferRequestList st={st} filtered={filtered} branches={branches} dispatch={dispatch} />

      <main className={styles.right}>
        {canEditAnyBranch && (
          <div className={trStyles.rightTopBar}>
            <button className={styles.addBtn} onClick={() => void openCreate()}>
              + New Transfer Request
            </button>
          </div>
        )}

        {!selected ? (
          <div className={styles.emptyRight}>
            <div className={styles.emptyRightCard}>
              <div className={styles.emptyRightIcon}>
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 12h14M12 5l7 7-7 7"/>
                </svg>
              </div>
              <p className={styles.emptyRightTitle}>No request selected</p>
              <p className={styles.emptyRightMsg}>
                Select a transfer request from the list to view its status, timeline, and available actions.
              </p>
            </div>
          </div>
        ) : (
          <TransferDetail
            req={selected}
            canApproveSource={canApproveSource}
            canDecideTarget={canDecideTarget}
            canComplete={canActOnEitherBranch}
            canCancel={canActOnEitherBranch}
            canReschedule={canActOnEitherBranch}
            sourceTimeZoneId={branches.find(b => b.branch_id === selected.source_branch_id)?.time_zone_id}
            actionLoading={st.actionLoading}
            actionError={st.actionError}
            onApproveSource={() => dispatch({ type: 'APPROVE_OPEN' })}
            onDecide={(d) => dispatch({ type: 'DECIDE_OPEN', decision: d })}
            onComplete={doComplete}
            onCancel={() => dispatch({ type: 'CANCEL_OPEN' })}
            onReschedule={() => dispatch({ type: 'RESCHEDULE_OPEN' })}
            onClearError={() => dispatch({ type: 'ACTION_ERR', err: null })}
          />
        )}
      </main>
      {st.toast && <div className={styles.toast}>{st.toast}</div>}

      <TransferRequestDialogs
        st={st} selected={selected} branches={branches} dispatch={dispatch}
        submitCreate={submitCreate} submitApproveSource={submitApproveSource}
        submitDecide={submitDecide} submitCancel={submitCancel} submitReschedule={submitReschedule}
      />
    </div>
  );
}
