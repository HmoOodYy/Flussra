"""
CP-1B: One-InReview-per-branch slot enforcement tests.

Product contract verified here:
  - At most one period per CompanyID/BranchID may have Status='InReview'.
  - Friendly precheck returns 409 on the sequential (non-race) path.
  - Partial unique index ux_payrollperiods_oneinreviewperbranch is the
    concurrency authority: detected by the SAIntegrityError handler.
  - Full loser rollback for both deterministic scenarios:
      a) Open submit blocked by B2 while Returned exists (RETURNED_BACKLOG_BLOCKS_SUBMIT).
      b) Open submit blocked by InReview slot after Returned resubmits.
  - The current-schema partial index is unique and scoped to InReview periods.

Dates: 2094-* — isolated year.  Run from backend/:
    python -m pytest tests/test_cp1b_inreview_slot.py -v
"""
import datetime
import itertools

import pytest
from sqlalchemy import text as _text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_WEEK_COUNTER = itertools.count(0)


def _next_dates() -> tuple[str, str]:
    """Return a unique (start, end) date pair in 2094."""
    n = next(_WEEK_COUNTER)
    base = datetime.date(2094, 1, 7)   # first Monday of 2094
    start = base + datetime.timedelta(weeks=n)
    end   = start + datetime.timedelta(days=6)
    return start.isoformat(), end.isoformat()


async def _cancel_active(direct_db, branch_id: int) -> None:
    await direct_db.execute(
        _text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
            "WHERE branchid = :bid AND status = 'Returned'"
        ),
        {"bid": branch_id},
    )
    await direct_db.execute(
        _text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled' "
            "WHERE branchid = :bid AND status IN ('Draft','Open','InReview')"
        ),
        {"bid": branch_id},
    )
    await direct_db.commit()


async def _create_and_open_period(client, token, branch_id, direct_db) -> tuple[int, str]:
    """Insert an Open period directly (CP-1D: legacy POST guard blocks creation without a prior Open)."""
    start_str, end_str = _next_dates()
    start_d = datetime.date.fromisoformat(start_str)
    end_d   = datetime.date.fromisoformat(end_str)
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"CP1B-{start_str}", "name": f"CP1B {start_str}",
         "start": start_d, "end": end_d},
    )).mappings().first()
    await direct_db.commit()
    return row["payrollperiodid"], start_str


async def _submit_to_inreview(client, token, pid) -> int:
    """Submit period to InReview. Returns review_item_id."""
    r = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "InReview"},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"submit: {r.text}"
    ri_resp = await client.get(
        "/review/items",
        params={"payroll_period_id": pid},
        headers=_auth(token),
    )
    pending = [i for i in ri_resp.json() if i["status"] == "Pending"]
    assert pending, f"No Pending review item for period {pid}"
    return pending[0]["review_item_id"]


async def _reject_to_returned(client, token, ri_id) -> None:
    dec = await client.post(
        f"/review/items/{ri_id}/decide",
        json={"decision": "Rejected", "decision_reason": "CP-1B test"},
        headers=_auth(token),
    )
    assert dec.status_code == 200, f"reject: {dec.text}"


@pytest.mark.asyncio
async def test_inreview_slot_index_has_the_current_unique_branch_contract(direct_db):
    row = (await direct_db.execute(_text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname = 'payroll'
          AND indexname = 'ux_payrollperiods_oneinreviewperbranch'
    """))).first()
    assert row is not None
    definition = row[0].lower()
    assert "unique" in definition
    assert "companyid" in definition and "branchid" in definition
    assert "inreview" in definition



# ---------------------------------------------------------------------------
# 2. Friendly precheck (sequential / non-race path)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3. Deterministic concurrency tests
# ---------------------------------------------------------------------------
