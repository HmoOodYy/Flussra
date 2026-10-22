import type { Dispatch } from 'react';
import type { DriverTransferRequest } from '../../../types/transfer';
import { statusKind, statusLabel, TRANSFER_ACTIVE } from '../../../types/transfer';
import { FILTER_LABELS, fmtEffectiveAt, type FilterView } from './transferRequestsView';
import { driverLabel, terminationWarning } from './transferActions';
import type { Branch } from '../../../types/core';
import type { TabAction, TabState } from './transferRequestsState';
import styles from '../PeoplePage.module.css';
import trStyles from './TransferRequests.module.css';

interface Props {
  st: Pick<TabState, 'requests' | 'selectedId' | 'search' | 'filter'>;
  filtered: DriverTransferRequest[];
  branches: Branch[];
  dispatch: Dispatch<TabAction>;
}

export function TransferRequestList({ st, filtered, branches, dispatch }: Props) {
  return (
      <aside className={styles.left}>
        <div className={styles.filters}>
          <input
            className={styles.search}
            placeholder="Search driver, branch…"
            value={st.search}
            onChange={e => dispatch({ type: 'SEARCH', val: e.target.value })}
          />
          {/* Filter pills — horizontal scroll */}
          <div className={trStyles.filterPills}>
            {(Object.keys(FILTER_LABELS) as FilterView[]).map(f => (
              <button
                key={f}
                className={`${trStyles.pill}${st.filter === f ? ` ${trStyles.pillActive}` : ''}`}
                onClick={() => dispatch({ type: 'FILTER', val: f })}
              >
                {FILTER_LABELS[f]}
                {f === 'active' && (
                  <span className={trStyles.pillCount}>
                    {st.requests.filter(r => TRANSFER_ACTIVE.has(r.status)).length}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>

        <div className={styles.list}>
          {filtered.length === 0 && (
            <div className={styles.emptyList}>
              {st.requests.length === 0
                ? 'No transfer requests found.'
                : 'No requests match your filters.'}
            </div>
          )}
          {filtered.map(req => (
            <TransferCard
              key={req.transfer_request_id}
              req={req}
              active={st.selectedId === req.transfer_request_id}
              warning={terminationWarning(
                req, branches.find(b => b.branch_id === req.source_branch_id)?.time_zone_id)}
              onClick={() => dispatch({ type: 'SELECT', id: req.transfer_request_id })}
            />
          ))}
        </div>
        <div className={styles.listFoot}>{filtered.length} / {st.requests.length} requests</div>
      </aside>
  );
}

function TransferCard({
  req,
  active,
  warning,
  onClick,
}: {
  req: DriverTransferRequest;
  active: boolean;
  warning: string | null;
  onClick(): void;
}) {
  const kind = statusKind(req.status);
  return (
    <button
      className={`${styles.item}${active ? ` ${styles.itemActive}` : ''}`}
      onClick={onClick}
    >
      {/* Direction icon */}
      <div className={trStyles.cardIcon}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 12h14M12 5l7 7-7 7"/>
        </svg>
      </div>

      <div className={styles.itemBody}>
        <div className={styles.itemName}>
          {driverLabel(req)}
          <span className={`${trStyles.badge} ${trStyles[`badge_${kind}`]}`}>
            {statusLabel(req.status)}
          </span>
        </div>
        <div className={styles.itemMeta}>
          {req.source_branch_name ?? `Branch ${req.source_branch_id}`}
          {' → '}
          {req.target_branch_name ?? `Branch ${req.target_branch_id}`}
        </div>
        <div className={styles.itemRow}>
          <span className={styles.scopeChip}>
            {req.initiated_by === 'Driver' ? '🧑 Driver request' : '🏢 Branch initiated'}
          </span>
          <span className={styles.scopeChip}>Effective {fmtEffectiveAt(req.effective_at)}</span>
        </div>
        {warning && <div className={styles.itemMeta}>{warning}</div>}
      </div>
    </button>
  );
}
