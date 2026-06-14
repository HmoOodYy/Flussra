/**
 * ReviewPage — payroll period review queue.
 *
 * Shows InReview payroll periods that have a Pending review item.
 * Reviewers can open each period for a detailed review and approve
 * or return it for corrections.
 *
 * Security:
 * - Backend enforces all authorization (403 for ODA/driver users).
 * - Branch scoping is enforced by the backend and reflected in the filter.
 */
import { useEffect, useState, useCallback } from 'react';
import apiClient from '../lib/apiClient';
import { getReviewItems } from '../lib/reviewApi';
import { getPeriods } from '../lib/payrollApi';
import type { ReviewItemSummary } from '../types/review';
import type { PeriodSummary } from '../types/payroll';
import type { Branch } from '../types/core';
import { ReviewDetailDialog } from './ReviewDetailDialog';
import { useAuth } from '../store/authStore';
import styles from './ReviewPage.module.css';

interface ReviewCard {
  item: ReviewItemSummary;
  period: PeriodSummary;
}

function fmtDate(d: string): string {
  const dt = new Date(d + 'T00:00:00');
  return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function ReviewPage() {
  const { user } = useAuth();
  const isAllBranches = user?.scope_type === 'AllCompanyBranches';

  const [branches, setBranches] = useState<Branch[]>([]);
  const [filterBranchId, setFilterBranchId] = useState<string>('');

  const [cards, setCards] = useState<ReviewCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openCard, setOpenCard] = useState<ReviewCard | null>(null);

  // Load branch list for AllCompanyBranches users
  useEffect(() => {
    if (!isAllBranches) return;
    apiClient.get<Branch[]>('/core/branches').then(r => setBranches(r.data)).catch(() => {});
  }, [isAllBranches]);

  const branchParam = isAllBranches
    ? (filterBranchId ? Number(filterBranchId) : undefined)
    : (user?.branch_ids?.[0]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [periods, items] = await Promise.all([
        getPeriods('InReview', branchParam),
        getReviewItems('Pending', branchParam),
      ]);

      // Match each InReview period to its Pending PeriodApproval review item
      const itemsByPeriodId = new Map(
        items
          .filter(i => i.entity_name === 'PayrollPeriods' && i.entity_schema === 'payroll')
          .map(i => [Number(i.entity_id), i]),
      );

      const matched: ReviewCard[] = periods
        .map(p => {
          const it = itemsByPeriodId.get(p.payroll_period_id);
          return it ? { item: it, period: p } : null;
        })
        .filter((c): c is ReviewCard => c !== null);

      setCards(matched);
    } catch (e: unknown) {
      const msg =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Failed to load review queue.';
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [branchParam]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  function handleDecided() {
    setOpenCard(null);
    void load();
  }

  const fixedBranchName = !isAllBranches
    ? (branches.find(b => b.branch_id === user?.branch_ids?.[0])?.branch_name ?? 'Your branch')
    : null;

  return (
    <div className={styles.page}>

      {/* Header */}
      <div className={styles.headerRow}>
        <div className={styles.titleBlock}>
          <h1 className={styles.pageTitle}>Review</h1>
          <span className={styles.pageSubtitle}>Payroll periods awaiting approval</span>
        </div>

        {isAllBranches ? (
          <div className={styles.filters}>
            <label className={styles.filterLabel}>
              Branch
              <select
                className={styles.filterSelect}
                value={filterBranchId}
                onChange={e => setFilterBranchId(e.target.value)}
              >
                <option value="">All branches</option>
                {branches.map(b => (
                  <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>
                ))}
              </select>
            </label>
          </div>
        ) : (
          <span className={styles.fixedBranch}>{fixedBranchName}</span>
        )}
      </div>

      {/* Content */}
      {loading ? (
        <div className={styles.loadingMsg}>Loading review queue…</div>
      ) : error ? (
        <p className={styles.errorMsg}>{error}</p>
      ) : cards.length === 0 ? (
        <div className={styles.emptyState}>
          <p className={styles.emptyTitle}>No periods pending review</p>
          <p className={styles.emptyHint}>
            Periods submitted for review (InReview status) will appear here.
          </p>
        </div>
      ) : (
        <div className={styles.cardList}>
          {cards.map(({ item, period }) => {
            const hasAttention = period.draft_lines_needing_attention > 0;
            return (
              <div key={item.review_item_id} className={styles.card}>
                <div className={styles.cardInfo}>
                  <div className={styles.cardNameRow}>
                    <span className={styles.cardName}>{period.period_name}</span>
                    <span className={styles.reviewBadge}>InReview</span>
                  </div>
                  <div className={styles.cardMeta}>
                    <span>{period.branch_name}</span>
                    <span className={styles.metaSep}>·</span>
                    <span>{period.period_type}</span>
                    <span className={styles.metaSep}>·</span>
                    <span>{fmtDate(period.start_date)} – {fmtDate(period.end_date)}</span>
                    {period.pay_date && (
                      <>
                        <span className={styles.metaSep}>·</span>
                        <span>Pay {fmtDate(period.pay_date)}</span>
                      </>
                    )}
                    {item.requested_by && (
                      <>
                        <span className={styles.metaSep}>·</span>
                        <span>Submitted by {item.requested_by}</span>
                      </>
                    )}
                  </div>
                  <div className={styles.cardStats}>
                    <span className={styles.statChip}>
                      <span className={styles.statLabel}>Drivers</span>
                      <span className={styles.statValue}>{period.draft_drivers}</span>
                    </span>
                    <span className={styles.statChip}>
                      <span className={styles.statLabel}>Lines</span>
                      <span className={styles.statValue}>{period.draft_lines}</span>
                    </span>
                    <span className={`${styles.statChip}${hasAttention ? ' ' + styles.statChipWarn : ''}`}>
                      <span className={styles.statLabel}>Needs Attention</span>
                      <span className={styles.statValue}>
                        {period.draft_lines_needing_attention}
                      </span>
                    </span>
                  </div>
                </div>
                <div className={styles.cardActions}>
                  <button
                    className={styles.openBtn}
                    onClick={() => setOpenCard({ item, period })}
                  >
                    Open Review
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Review detail dialog */}
      {openCard && (
        <ReviewDetailDialog
          item={openCard.item}
          period={openCard.period}
          onClose={() => setOpenCard(null)}
          onDecided={handleDecided}
        />
      )}

    </div>
  );
}
