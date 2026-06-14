-- =============================================================================
-- Migration 0013: Partial unique index for Pending PeriodApproval review items
--
-- Prevents duplicate Pending PeriodApproval items for the same period at the
-- database level, complementing the service-layer duplicate guard.
--
-- The service guard alone is insufficient under concurrent requests: two
-- simultaneous Open-to-InReview submissions for the same period can both pass
-- the SELECT-based duplicate check before either INSERT commits.
--
-- Only one Pending PeriodApproval item per (companyid, entityid) is allowed.
-- EditRequested / Approved / Rejected / Cancelled items are exempt (historical).
-- =============================================================================

CREATE UNIQUE INDEX ux_ReviewItems_OnePendingPeriodApproval
    ON review.managerreviewitems (companyid, entityid)
    WHERE requesttype = 'PeriodApproval'
      AND status      = 'Pending';
