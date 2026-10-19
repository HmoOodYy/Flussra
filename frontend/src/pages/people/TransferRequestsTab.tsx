import type { Branch } from '../../types/core';
import { useTransferRequests } from './transfers/useTransferRequests';
import { TransferRequestList } from './transfers/TransferRequestList';
import { TransferDetail } from './transfers/TransferDetail';
import { TransferRequestDialogs } from './transfers/TransferRequestDialogs';
import styles from './PeoplePage.module.css';
import trStyles from './TransferRequestsTab.module.css';

interface Props {
  branches: Branch[];
}

export function TransferRequestsTab({ branches }: Props) {
  const {
    st, dispatch, canEditAnyBranch, filtered, selected,
    canApproveSource, canDecideTarget, canActOnEitherBranch,
    openCreate, submitCreate, submitApproveSource, submitDecide, doComplete, submitCancel,
    submitReschedule,
  } = useTransferRequests(branches);

  if (st.loading) return <div className={styles.splash}>Loading transfer requests…</div>;
  if (st.loadErr) return <div className={styles.splash} style={{ color: '#dc2626' }}>Failed to load: {st.loadErr}</div>;

  return (
    <div className={styles.body}>
      <TransferRequestList st={st} filtered={filtered} branches={branches} dispatch={dispatch} />

      {/* ── Right panel ── */}
      <main className={styles.right}>
        {/* Create button bar (top of right panel) */}
        {canEditAnyBranch && (
          <div className={trStyles.rightTopBar}>
            <button className={styles.addBtn} onClick={openCreate}>
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
      {/* ── Toast ── */}
      {st.toast && <div className={styles.toast}>{st.toast}</div>}

      <TransferRequestDialogs
        st={st} selected={selected} branches={branches} dispatch={dispatch}
        submitCreate={submitCreate} submitApproveSource={submitApproveSource}
        submitDecide={submitDecide} submitCancel={submitCancel} submitReschedule={submitReschedule}
      />
    </div>
  );
}