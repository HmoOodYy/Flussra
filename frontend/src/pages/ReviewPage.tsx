/**
 * ReviewPage - payroll PeriodApproval queue and historical review items.
 *
 * ReviewItem state is the queue authority. Financial detail is loaded only
 * from the immutable snapshot linked to the selected ReviewItem.
 */
import { useEffect, useState, useCallback } from 'react';
import apiClient from '../lib/apiClient';
import { getReviewItems } from '../lib/reviewApi';
import type { ReviewItemSummary } from '../types/review';
import type { Branch } from '../types/core';
import { ReviewDetailDialog } from './ReviewDetailDialog';
import { useAuth } from '../store/authStore';
import styles from './ReviewPage.module.css';

type QueueView = 'active' | 'history';

function fmtTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : date.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function statusLabel(status: string): string {
  if (status === 'EditRequested') return 'Returned for Correction';
  return status;
}

function isPeriodApproval(item: ReviewItemSummary): boolean {
  return item.request_type === 'PeriodApproval' && item.entity_schema === 'payroll' && item.entity_name === 'PayrollPeriods';
}

export function ReviewPage() {
  const { user } = useAuth();
  const isAllBranches = user?.scope_type === 'AllCompanyBranches';

  const [branches, setBranches] = useState<Branch[]>([]);
  const [filterBranchId, setFilterBranchId] = useState<string>('');
  const [queueView, setQueueView] = useState<QueueView>('active');
  const [items, setItems] = useState<ReviewItemSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openItem, setOpenItem] = useState<ReviewItemSummary | null>(null);

  useEffect(() => {
    if (!isAllBranches) return;
    apiClient.get<Branch[]>('/core/branches').then((response) => setBranches(response.data)).catch(() => {});
  }, [isAllBranches]);

  const branchParam = isAllBranches
    ? (filterBranchId ? Number(filterBranchId) : undefined)
    : user?.branch_ids?.[0];

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const reviewItems = await getReviewItems(undefined, branchParam);
      setItems(reviewItems.filter(isPeriodApproval));
    } catch (loadError: unknown) {
      const detail = (loadError as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Failed to load review queue.');
    } finally {
      setLoading(false);
    }
  }, [branchParam]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  function handleDecided() {
    setOpenItem(null);
    void load();
  }

  const activeItems = items.filter((item) => item.status === 'Pending');
  const historicalItems = items.filter((item) => item.status !== 'Pending');
  const visibleItems = queueView === 'active' ? activeItems : historicalItems;
  const fixedBranchName = !isAllBranches
    ? (branches.find((branch) => branch.branch_id === user?.branch_ids?.[0])?.branch_name ?? 'Your branch')
    : null;

  return (
    <div className={styles.page}>
      <div className={styles.headerRow}>
        <div className={styles.titleBlock}>
          <h1 className={styles.pageTitle}>Review</h1>
          <span className={styles.pageSubtitle}>Submitted payroll snapshots awaiting review</span>
        </div>

        {isAllBranches ? (
          <div className={styles.filters}>
            <label className={styles.filterLabel}>
              Branch
              <select
                className={styles.filterSelect}
                value={filterBranchId}
                onChange={(event) => setFilterBranchId(event.target.value)}
              >
                <option value="">All branches</option>
                {branches.map((branch) => (
                  <option key={branch.branch_id} value={branch.branch_id}>{branch.branch_name}</option>
                ))}
              </select>
            </label>
          </div>
        ) : (
          <span className={styles.fixedBranch}>{fixedBranchName}</span>
        )}
      </div>

      <div className={styles.tabs} role="tablist" aria-label="Review queue">
        <button
          className={queueView === 'active' ? styles.tabActive : styles.tab}
          onClick={() => setQueueView('active')}
          role="tab"
          aria-selected={queueView === 'active'}
        >
          Pending ({activeItems.length})
        </button>
        <button
          className={queueView === 'history' ? styles.tabActive : styles.tab}
          onClick={() => setQueueView('history')}
          role="tab"
          aria-selected={queueView === 'history'}
        >
          History ({historicalItems.length})
        </button>
      </div>

      {loading ? (
        <div className={styles.loadingMsg}>Loading review queue...</div>
      ) : error ? (
        <p className={styles.errorMsg}>{error}</p>
      ) : visibleItems.length === 0 ? (
        <div className={styles.emptyState}>
          <p className={styles.emptyTitle}>{queueView === 'active' ? 'No periods pending review' : 'No historical payroll reviews'}</p>
          <p className={styles.emptyHint}>
            {queueView === 'active'
              ? 'Submitted payroll snapshots will appear here when they need a decision.'
              : 'Returned and approved review decisions remain available here as history.'}
          </p>
        </div>
      ) : (
        <div className={styles.cardList}>
          {visibleItems.map((item) => {
            const isPending = item.status === 'Pending';
            return (
              <article key={item.review_item_id} className={styles.card}>
                <div className={styles.cardInfo}>
                  <div className={styles.cardNameRow}>
                    <span className={styles.cardName}>{item.title}</span>
                    <span className={isPending ? styles.pendingBadge : styles.historyBadge}>{statusLabel(item.status)}</span>
                  </div>
                  <div className={styles.cardMeta}>
                    <span>{item.branch_name ?? 'Branch unavailable'}</span>
                    <span className={styles.metaSep}>/</span>
                    <span>Submitted {fmtTimestamp(item.created_at_utc)}</span>
                    {item.requested_by && <><span className={styles.metaSep}>/</span><span>Submitted by {item.requested_by}</span></>}
                    {item.entity_id && <><span className={styles.metaSep}>/</span><span>Payroll period #{item.entity_id}</span></>}
                  </div>
                  {item.final_decision_reason && (
                    <p className={styles.decisionReason}>{item.final_decision_reason}</p>
                  )}
                </div>
                <div className={styles.cardActions}>
                  <button className={styles.openBtn} onClick={() => setOpenItem(item)}>
                    {isPending ? 'Open Review' : 'View History'}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {openItem && (
        <ReviewDetailDialog
          item={openItem}
          onClose={() => setOpenItem(null)}
          onDecided={handleDecided}
        />
      )}
    </div>
  );
}
