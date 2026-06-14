"""0024: Add FinalGross and FinalDriverCount to vw_PayrollPeriodList.

The Ledger page needs per-period total final gross pay and driver count for
finalized (Locked/Archived) periods without issuing per-row summary queries.
These aggregates are computed in the existing fl_agg subquery that already
provides FinalLines count, so this is a safe CREATE OR REPLACE with no
data loss or destructive DDL.

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0024"
down_revision: str = "0023"
branch_labels = None
depends_on = None


_UP = """
CREATE OR REPLACE VIEW app.vw_PayrollPeriodList AS
SELECT
    p.CompanyID,
    p.BranchID,
    b.BranchName,
    p.PayrollPeriodID,
    p.ParentPayrollPeriodID,
    p.PeriodCode,
    p.PeriodName,
    p.PeriodType,
    p.StartDate,
    p.EndDate,
    p.Status,
    COALESCE(dl_agg.DraftDrivers,               0)    AS DraftDrivers,
    COALESCE(dl_agg.DraftLines,                  0)    AS DraftLines,
    COALESCE(dl_agg.DraftLinesNeedingAttention,  0)    AS DraftLinesNeedingAttention,
    COALESCE(fl_agg.FinalLines,                  0)    AS FinalLines,
    COALESCE(fl_agg.FinalGross,                  0)    AS FinalGross,
    COALESCE(fl_agg.FinalDriverCount,            0)    AS FinalDriverCount
FROM payroll.PayrollPeriods AS p
JOIN core.Branches AS b ON b.BranchID = p.BranchID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(DISTINCT DriverID)                                                                            AS DraftDrivers,
        COUNT(DraftLineID)                                                                                  AS DraftLines,
        SUM(CASE WHEN Status IN ('NeedsReview','Rejected') OR NeedsManagerReview THEN 1 ELSE 0 END)        AS DraftLinesNeedingAttention
    FROM payroll.PayrollDraftLines
    GROUP BY PayrollPeriodID
) AS dl_agg ON dl_agg.PayrollPeriodID = p.PayrollPeriodID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(FinalLineID)          AS FinalLines,
        SUM(FinalAmount)            AS FinalGross,
        COUNT(DISTINCT DriverID)    AS FinalDriverCount
    FROM payroll.PayrollFinalLines
    GROUP BY PayrollPeriodID
) AS fl_agg ON fl_agg.PayrollPeriodID = p.PayrollPeriodID
"""

# Roll back to the 0023 view definition (FinalLines only in fl_agg)
_DOWN = """
CREATE OR REPLACE VIEW app.vw_PayrollPeriodList AS
SELECT
    p.CompanyID,
    p.BranchID,
    b.BranchName,
    p.PayrollPeriodID,
    p.ParentPayrollPeriodID,
    p.PeriodCode,
    p.PeriodName,
    p.PeriodType,
    p.StartDate,
    p.EndDate,
    p.Status,
    COALESCE(dl_agg.DraftDrivers,               0)    AS DraftDrivers,
    COALESCE(dl_agg.DraftLines,                  0)    AS DraftLines,
    COALESCE(dl_agg.DraftLinesNeedingAttention,  0)    AS DraftLinesNeedingAttention,
    COALESCE(fl_agg.FinalLines,                  0)    AS FinalLines
FROM payroll.PayrollPeriods AS p
JOIN core.Branches AS b ON b.BranchID = p.BranchID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(DISTINCT DriverID)                                                                            AS DraftDrivers,
        COUNT(DraftLineID)                                                                                  AS DraftLines,
        SUM(CASE WHEN Status IN ('NeedsReview','Rejected') OR NeedsManagerReview THEN 1 ELSE 0 END)        AS DraftLinesNeedingAttention
    FROM payroll.PayrollDraftLines
    GROUP BY PayrollPeriodID
) AS dl_agg ON dl_agg.PayrollPeriodID = p.PayrollPeriodID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(FinalLineID) AS FinalLines
    FROM payroll.PayrollFinalLines
    GROUP BY PayrollPeriodID
) AS fl_agg ON fl_agg.PayrollPeriodID = p.PayrollPeriodID
"""


def upgrade() -> None:
    op.execute(sa.text(_UP))


def downgrade() -> None:
    op.execute(sa.text(_DOWN))
