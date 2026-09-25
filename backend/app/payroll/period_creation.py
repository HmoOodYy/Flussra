"""Candidate-based payroll period creation from persisted Payroll Setup authority.

The canonical resolver determines dates and exact Assignment/Version authority.
Confirmation revalidates its signed candidate under the Branch workflow lock and
atomically freezes Period, PeriodDay, snapshot, and audit evidence. Legacy direct
creation and next-date entry points are disabled pending physical retirement.

The slot matrix is shared with Current Payroll Hub.
"""
import base64
import calendar as _calendar
import hashlib
import hmac as _hmac_mod
import json
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import settings
from app.core.service import (
    _check_any_permission,
    _check_branch_access,
    _check_permission,
    _require_not_driver_role,
)
from app.payroll.audit_evidence import initialize_period_audit_evidence_coverage
from app.payroll.eligibility import _create_period_driver_eligibility_rows
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import (
    CandidateNavigationInfo,
    CandidatePreviewResponse,
    CandidateSelectedInfo,
    NextPeriodDates,
    PeriodCreate,
    PeriodCreationRequest,
    PeriodCreationResponse,
    PeriodSummary,
)
from app.payroll.workflow_lock import _acquire_branch_workflow_lock
from app.payroll_setup.audit import write_policy_audit
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.resolver import Authority, resolve_payroll_setup_version

# ---------------------------------------------------------------------------
# CP-1C: Branch-locked candidate-based period creation
# ---------------------------------------------------------------------------

# Active slot statuses that govern mode-eligibility checks:
_ACTIVE_SLOT_STATUSES = frozenset({"Draft", "Open", "InReview", "Returned"})

_CP1C_VERSION = "cp1c-v1"
_CP1C_PURPOSE = "period_creation"
_CP1C_MAX_FUTURE = 12


def _cp1c_error(code: str, message: str, http_status: int = 409) -> None:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
    )


# ---------------------------------------------------------------------------
# Helper: slot matrix
# ---------------------------------------------------------------------------

def _check_slot_matrix(
    mode: str,
    periods: list[dict],
) -> tuple[bool, str | None]:
    """
    Apply the mode/slot matrix.
    Returns (creatable, error_code | None).
    """
    counts: dict[str, int] = {}
    for p in periods:
        s = p["status"]
        if s in _ACTIVE_SLOT_STATUSES:
            counts[s] = counts.get(s, 0) + 1

    for s, cnt in counts.items():
        if cnt > 1:
            return False, "SLOT_INVARIANT_VIOLATION"

    has_open = "Open" in counts
    has_draft = "Draft" in counts

    if mode == "OPEN_CREATION":
        if has_open and has_draft:
            return False, "ACTIVE_PERIOD_SLOTS_FULL"
        if has_open:
            return False, "OPEN_FILLED"
        if has_draft:
            return False, "DRAFT_WITHOUT_OPEN"
        return True, None

    if mode == "PREPARED_CREATION":
        if has_open and has_draft:
            return False, "ACTIVE_PERIOD_SLOTS_FULL"
        if has_draft and not has_open:
            return False, "DRAFT_WITHOUT_OPEN"
        if not has_open:
            return False, "OPEN_REQUIRED"
        return True, None

    _cp1c_error("INVALID_CANDIDATE_KEY", f"Unknown mode: {mode!r}.")
    return False, None  # unreachable


# ---------------------------------------------------------------------------
# Shared period-naming / date-math primitives
#
# Used by both the candidate-based path below and the legacy create_period
# path (see the "Legacy period creation" section at the end of this module,
# since B4-21).
# ---------------------------------------------------------------------------

def _auto_period_name(period_type: str, start: date, end: date) -> str:
    """Generate a human-readable period name from its type and date range."""
    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    if period_type == "Week":
        return f"Week of {month_names[start.month]} {start.day}, {start.year}"
    if period_type == "Month":
        full_months = {
            1: "January", 2: "February", 3: "March", 4: "April",
            5: "May", 6: "June", 7: "July", 8: "August",
            9: "September", 10: "October", 11: "November", 12: "December",
        }
        return f"{full_months[start.month]} {start.year}"
    # Biweek or Custom
    return (
        f"{month_names[start.month]} {start.day}"
        f" – {month_names[end.month]} {end.day}, {end.year}"
    )


async def _unique_period_code(
    base: str,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> str:
    """
    Return `base` if it's not already used in this branch, otherwise
    try `base-2`, `base-3`, … until a free slot is found.
    """
    candidate = base
    suffix = 1
    while True:
        result = await db.execute(
            text("""
                SELECT 1 FROM payroll.payrollperiods
                WHERE  companyid = :cid
                  AND  branchid  = :bid
                  AND  periodcode = :code
                  AND  status    != 'Cancelled'
            """),
            {"cid": company_id, "bid": branch_id, "code": candidate},
        )
        if result.first() is None:
            return candidate
        suffix += 1
        candidate = f"{base}-{suffix}"


def _month_end(start: date) -> date:
    """
    Return the last day of a one-calendar-month period that starts on `start`.

    The exclusive boundary is the same day of the next month; we subtract one
    day to get the inclusive end:

      start = 2026-06-21  →  next_same_day = 2026-07-21  →  end = 2026-07-20
      start = 2026-01-31  →  next_same_day = 2026-02-28  →  end = 2026-02-27
    """
    month = start.month + 1
    year  = start.year + (1 if month > 12 else 0)
    if month > 12:
        month -= 12
    max_day = _calendar.monthrange(year, month)[1]
    next_month_same_day = start.replace(year=year, month=month, day=min(start.day, max_day))
    return next_month_same_day - timedelta(days=1)


# ---------------------------------------------------------------------------
# Helpers: HMAC signing / verification
# ---------------------------------------------------------------------------

def _make_candidate_key(payload: dict) -> tuple[str, str]:
    """
    Sign payload and return (candidate_key, candidate_hash).
    candidate_key  = base64url(canonical_json).hmac_sha256_hex
    candidate_hash = hmac_sha256_hex (64 hex chars stored in DB)
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    b64_part = base64.urlsafe_b64encode(canonical.encode()).decode().rstrip("=")
    sig = _hmac_mod.new(
        settings.SECRET_KEY.encode(),
        b64_part.encode(),
        "sha256",
    ).hexdigest()
    return f"{b64_part}.{sig}", sig


def _decode_candidate_key(key: str) -> tuple[dict, str]:
    """
    Decode and verify a candidate_key.
    Returns (payload_dict, hmac_hex).
    Raises HTTPException 409 INVALID_CANDIDATE_KEY on any failure.
    """
    try:
        b64_part, sig = key.rsplit(".", 1)
    except (ValueError, AttributeError):
        _cp1c_error("INVALID_CANDIDATE_KEY", "Malformed candidate key.")

    expected = _hmac_mod.new(
        settings.SECRET_KEY.encode(),
        b64_part.encode(),
        "sha256",
    ).hexdigest()
    if not _hmac_mod.compare_digest(expected, sig):
        _cp1c_error("INVALID_CANDIDATE_KEY", "Candidate key signature invalid.")

    try:
        padding = (4 - len(b64_part) % 4) % 4
        payload = json.loads(
            base64.urlsafe_b64decode(b64_part + "=" * padding).decode()
        )
    except Exception:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Candidate key payload unreadable.")

    if payload.get("ver") != _CP1C_VERSION or payload.get("purpose") != _CP1C_PURPOSE:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Wrong version or purpose in candidate key.")

    return payload, sig


# ---------------------------------------------------------------------------
# Helpers: fingerprints
# ---------------------------------------------------------------------------

def _setup_fingerprint(freq: str, anchor: date, interval_days: int | None) -> str:
    return json.dumps(
        {"anchor": str(anchor), "freq": freq, "interval": interval_days},
        sort_keys=True,
        separators=(",", ":"),
    )


def _slot_fingerprint(periods: list[dict]) -> str:
    """Deterministic fingerprint of all non-Cancelled periods (sorted status+id pairs)."""
    pairs = sorted(
        [(r["status"], r["payrollperiodid"])
         for r in periods
         if r["status"] != "Cancelled"],
        key=lambda x: (x[0], x[1]),
    )
    return json.dumps(pairs, separators=(",", ":"))


async def _next_candidate_start(
    company_id: int, branch_id: int, db: AsyncConnection,
) -> tuple[date, date | None]:
    """Derive the first candidate from non-Cancelled chronology or the first assignment."""
    result = await db.execute(text("""
        SELECT MAX(EndDate) FROM payroll.PayrollPeriods
        WHERE CompanyID = :cid AND BranchID = :bid AND Status <> 'Cancelled'
    """), {"cid": company_id, "bid": branch_id})
    last_end = result.scalar_one_or_none()
    if last_end is not None:
        return last_end + timedelta(days=1), last_end
    result = await db.execute(text("""
        SELECT MIN(EffectiveFromDate) FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
    """), {"cid": company_id, "bid": branch_id})
    first_start = result.scalar_one_or_none()
    if first_start is None:
        _cp1c_error("PAYROLL_SETUP_REQUIRED", "No persisted Payroll Setup assignment exists for this branch.")
    return first_start, None


async def _resolve_candidate(
    company_id: int, branch_id: int, start: date, db: AsyncConnection,
) -> Authority:
    try:
        return await resolve_payroll_setup_version(company_id, branch_id, start, db)
    except PolicyError as exc:
        _cp1c_error(exc.code, str(exc))


async def _candidate_at_offset(
    company_id: int, branch_id: int, first_start: date, offset: int,
    db: AsyncConnection,
) -> Authority:
    start = first_start
    for index in range(offset + 1):
        authority = await _resolve_candidate(company_id, branch_id, start, db)
        if index < offset:
            start = authority.end_date + timedelta(days=1)
    return authority


async def _timeline_fingerprints(
    company_id: int, branch_id: int, setup_id: int, db: AsyncConnection,
) -> tuple[str, str]:
    """Fingerprint persisted timelines plus append-only policy-event revisions."""
    versions = (await db.execute(text("""
        SELECT PayrollSetupVersionID, EffectiveFromDate, ConfigHash, ReplacesVersionID
        FROM payroll.PayrollSetupVersions
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
          AND LifecycleState = 'Published'
        ORDER BY PayrollSetupVersionID
    """), {"cid": company_id, "sid": setup_id})).all()
    assignments = (await db.execute(text("""
        SELECT BranchPayrollSetupAssignmentID, PayrollSetupID, EffectiveFromDate,
               EffectiveToDate, WithdrawnAtUtc
        FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid
        ORDER BY BranchPayrollSetupAssignmentID
    """), {"cid": company_id, "bid": branch_id})).all()
    setup_revision = (await db.execute(text("""
        SELECT MAX(PayrollSetupPolicyAuditEventID)
        FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
          AND EventType IN ('VersionPublished', 'FutureVersionScheduled', 'VersionReplaced')
    """), {"cid": company_id, "sid": setup_id})).scalar_one_or_none()
    branch_revision = (await db.execute(text("""
        SELECT MAX(PayrollSetupPolicyAuditEventID)
        FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND BranchID = :bid
          AND EventType IN ('BranchAssigned', 'BranchReassigned', 'AssignmentWithdrawn')
    """), {"cid": company_id, "bid": branch_id})).scalar_one_or_none()
    setup_state = [
        [row[0], str(row[1]), row[2], row[3]] for row in versions
    ]
    branch_state = [
        [row[0], row[1], str(row[2]), str(row[3]) if row[3] else None,
         row[4].isoformat() if row[4] else None]
        for row in assignments
    ]
    def fingerprint(rows: list, revision: int | None) -> str:
        payload = json.dumps([rows, revision], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
    return fingerprint(setup_state, setup_revision), fingerprint(branch_state, branch_revision)


# ---------------------------------------------------------------------------
# CP-2A: ensure current schedule version
# ---------------------------------------------------------------------------

async def ensure_current_schedule_version(
    company_id: int,
    branch_id: int,
    user_id: int | None,
    db: AsyncConnection,
) -> int | None:
    """
    Return the current ScheduleVersionID for this branch.

    Must be called while holding the branch workflow advisory lock.

    If BranchPayrollSettings.CurrentScheduleVersionID is already set and the
    referenced version row exists for the correct company/branch, return it.

    If it is NULL (e.g. setup pre-dates CP-2A migration and the backfill missed
    this row, which should not happen but is handled defensively), create a new
    repair version (SourceAction='REPAIR') and update CurrentScheduleVersionID.

    Returns None if there is no active setup row at all. Callers must treat
    None as PAYROLL_SETUP_REQUIRED — the same condition as a missing setup.

    Does not create versions for missing/inactive setup; period creation paths
    must reject PAYROLL_SETUP_REQUIRED before calling this.
    """
    setup_row = (await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays,
                   normaldaysoffmask, paydayofweek, firstpaydate,
                   includepaydayasworkday, currentscheduleversionid
            FROM   payroll.branchpayrollsettings
            WHERE  branchid = :bid AND companyid = :cid AND isactive = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()

    if setup_row is None:
        return None

    sv_id = setup_row.get("currentscheduleversionid")

    if sv_id is not None:
        # Verify the referenced version exists for this company/branch.
        exists = (await db.execute(
            text("""
                SELECT 1 FROM payroll.PayrollScheduleVersions
                WHERE scheduleversionid = :sv_id
                  AND companyid = :cid AND branchid = :bid
            """),
            {"sv_id": sv_id, "cid": company_id, "bid": branch_id},
        )).first()
        if exists is not None:
            return sv_id

    # CurrentScheduleVersionID is missing or points to a stale/wrong row.
    # Create a repair version from the current live setup values.
    freq = setup_row["payrollfrequency"]
    anchor = setup_row["anchorstartdate"]
    interval_days = setup_row.get("customintervaldays")
    config_hash = json.dumps(
        {"anchor": str(anchor), "freq": freq, "interval": interval_days},
        sort_keys=True,
        separators=(",", ":"),
    )

    max_row = await db.execute(
        text("""
            SELECT COALESCE(MAX(versionnumber), 0) AS maxver
            FROM   payroll.PayrollScheduleVersions
            WHERE  companyid = :cid AND branchid = :bid
        """),
        {"cid": company_id, "bid": branch_id},
    )
    next_version = (max_row.scalar_one() or 0) + 1

    ins = await db.execute(
        text("""
            INSERT INTO payroll.PayrollScheduleVersions
                (CompanyID, BranchID, VersionNumber,
                 PayrollFrequency, AnchorStartDate,
                 CustomIntervalDays, NormalDaysOffMask,
                 PayDayOfWeek, FirstPayDate, IncludePayDayAsWorkDay,
                 EffectiveFromDate, EffectiveToDate,
                 CreatedByUserID, SourceAction, ConfigHash)
            VALUES
                (:cid, :bid, :vnum,
                 :freq, :anchor,
                 :interval_days, :mask,
                 :pdow, :fpd, :incl,
                 :anchor, NULL,
                 :uid, 'REPAIR', :chash)
            RETURNING ScheduleVersionID
        """),
        {
            "cid":           company_id,
            "bid":           branch_id,
            "vnum":          next_version,
            "freq":          freq,
            "anchor":        anchor,
            "interval_days": interval_days,
            "mask":          setup_row.get("normaldaysoffmask"),
            "pdow":          setup_row.get("paydayofweek"),
            "fpd":           setup_row.get("firstpaydate"),
            "incl":          bool(setup_row.get("includepaydayasworkday") or False),
            "uid":           user_id,
            "chash":         config_hash,
        },
    )
    new_sv_id: int = ins.scalar_one()

    await db.execute(
        text("""
            UPDATE payroll.BranchPayrollSettings
            SET    CurrentScheduleVersionID = :sv_id
            WHERE  CompanyID = :cid AND BranchID = :bid
        """),
        {"sv_id": new_sv_id, "cid": company_id, "bid": branch_id},
    )

    return new_sv_id


# ---------------------------------------------------------------------------
# Helper: candidate date computation at offset N
# ---------------------------------------------------------------------------

def _candidate_dates_at_offset(
    frequency: str,
    anchor: date,
    interval_days: int | None,
    pred_end: date | None,
    offset: int,
) -> tuple[date, date]:
    """Compute (start, end) for the candidate at position `offset` from current."""
    if pred_end is None:
        start = anchor
    else:
        start = max(anchor, pred_end + timedelta(days=1))

    end = _period_end(frequency, start, interval_days)

    for _ in range(offset):
        start = end + timedelta(days=1)
        end = _period_end(frequency, start, interval_days)

    return start, end


# ---------------------------------------------------------------------------
# CP-2B: Period-day snapshot helpers
# ---------------------------------------------------------------------------

def _generate_period_day_rows(
    start_date: date,
    end_date: date,
    normal_days_off_mask: int | None,
) -> list[dict]:
    """
    Return one dict per calendar day from start_date through end_date inclusive.

    DayOfWeek uses Sun=0 … Sat=6 to match NormalDaysOffMask bit positions
    (bit 0=Sun, bit 1=Mon, …, bit 6=Sat).

    Conversion from Python date.weekday() (Mon=0 … Sun=6):
        day_of_week = (python_weekday + 1) % 7
    """
    rows = []
    current = start_date
    while current <= end_date:
        py_wd = current.weekday()          # Mon=0 … Sun=6
        day_of_week = (py_wd + 1) % 7     # Sun=0 … Sat=6

        mask = normal_days_off_mask or 0
        is_configured_off = bool(mask & (1 << day_of_week))
        rows.append({
            "work_date":           current,
            "day_of_week":         day_of_week,
            "is_default_work_day": not is_configured_off,
            "is_configured_off_day": is_configured_off,
        })
        current += timedelta(days=1)
    return rows


async def _create_period_day_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    assignment_id: int,
    setup_version_id: int,
    start_date: date,
    end_date: date,
    normal_days_off_mask: int | None,
    db: AsyncConnection,
) -> None:
    """
    Insert one PayrollPeriodDays row per calendar day for the given period.
    Called inside the period-creation transaction, still under the branch advisory lock.
    ON CONFLICT DO NOTHING ensures idempotency (candidate replay guard).
    The exact new authority is enforced against the parent Period by composite FKs.
    """
    day_rows = _generate_period_day_rows(start_date, end_date, normal_days_off_mask)
    if not day_rows:
        return

    values_sql = ", ".join(
        f"(:pid, :cid, :bid, :aid, :vid, :wd_{i}, :dow_{i}, :isd_{i}, :ico_{i})"
        for i in range(len(day_rows))
    )
    params: dict = {"pid": period_id, "cid": company_id, "bid": branch_id,
                    "aid": assignment_id, "vid": setup_version_id}
    for i, row in enumerate(day_rows):
        params[f"wd_{i}"]  = row["work_date"]
        params[f"dow_{i}"] = row["day_of_week"]
        params[f"isd_{i}"] = row["is_default_work_day"]
        params[f"ico_{i}"] = row["is_configured_off_day"]

    await db.execute(
        text(f"""
            INSERT INTO payroll.PayrollPeriodDays
                (PayrollPeriodID, CompanyID, BranchID,
                 BranchPayrollSetupAssignmentID, PayrollSetupVersionID,
                 WorkDate, DayOfWeek, IsDefaultWorkDay, IsConfiguredOffDay)
            VALUES {values_sql}
            ON CONFLICT (PayrollPeriodID, WorkDate) DO NOTHING
        """),
        params,
    )


# ---------------------------------------------------------------------------
# CP-2C: Period pay-item layout snapshot helpers
# ---------------------------------------------------------------------------

async def _create_period_pay_item_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    start_date: date,
    db: AsyncConnection,
) -> None:
    """
    Insert one PayrollPeriodPayItems row per non-Retired PayItem (system +
    company custom) as of start_date.

    Branch activation is resolved from BranchPayItemConfig using start_date.
    Both Daily and Period scope items are snapshotted.
    DailyStatus / DailyNote pseudo-lines are excluded (no PayItems catalog row).

    Called once at period creation; rows are immutable afterward.
    """
    items_result = await db.execute(
        text("""
            SELECT
                pi.payitemid,
                pi.payitemcode,
                pi.payitemname,
                pi.displaylabel,
                pi.category,
                pi.datatype,
                pi.unit,
                pi.itemscope,
                pi.ratebehavior,
                pi.appearsinpayrollentry,
                pi.appearsinledger,
                pi.appearsinreports,
                pi.requiresrate,
                pi.issystemstandard,
                (pi.companyid IS NOT NULL) AS iscustom,
                pi.status,
                pi.sortorder,
                pi.isdefaultbranchactive,
                bpic.isactive          AS cfg_isactive,
                bpic.effectivefrom     AS cfg_effectivefrom,
                bpic.configid          AS cfg_configid
            FROM payroll.payitems pi
            LEFT JOIN payroll.branchpayitemconfig bpic
                   ON bpic.payitemid      = pi.payitemid
                  AND bpic.companyid      = :cid
                  AND bpic.branchid       = :bid
                  AND bpic.effectivefrom <= :dt
                  AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :dt)
            WHERE (pi.companyid IS NULL OR pi.companyid = :cid)
              AND pi.status != 'Retired'
            ORDER BY pi.sortorder NULLS LAST, pi.payitemcode
        """),
        {"cid": company_id, "bid": branch_id, "dt": start_date},
    )
    rows = items_result.mappings().all()
    if not rows:
        return

    for r in rows:
        is_active_in_period = bool(
            r["cfg_isactive"] if r["cfg_isactive"] is not None else r["isdefaultbranchactive"]
        )
        await db.execute(
            text("""
                INSERT INTO payroll.payrollperiodpayitems
                    (payrollperiodid, companyid, branchid, payitemid,
                     payitemcode, payitemname, displaylabel,
                     category, datatype, unit,
                     itemscope, ratebehavior,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, iscustom,
                     payitemstatusatsnapshot, isactiveinperiod, sortorder,
                     snapshoteffectivefrom, sourcebranchpayitemconfigid,
                     createdatutc)
                VALUES
                    (:period_id, :cid, :bid, :payitemid,
                     :payitemcode, :payitemname, :displaylabel,
                     :category, :datatype, :unit,
                     :itemscope, :ratebehavior,
                     :appearsinpayrollentry, :appearsinledger, :appearsinreports,
                     :requiresrate, :issystemstandard, :iscustom,
                     :payitemstatus, :isactiveinperiod, :sortorder,
                     :snapshoteffectivefrom, :sourceconfigid,
                     NOW())
                ON CONFLICT (payrollperiodid, payitemid) DO NOTHING
            """),
            {
                "period_id":             period_id,
                "cid":                   company_id,
                "bid":                   branch_id,
                "payitemid":             r["payitemid"],
                "payitemcode":           r["payitemcode"],
                "payitemname":           r["payitemname"],
                "displaylabel":          r["displaylabel"],
                "category":              r["category"],
                "datatype":              r["datatype"],
                "unit":                  r["unit"],
                "itemscope":             r["itemscope"],
                "ratebehavior":          r["ratebehavior"],
                "appearsinpayrollentry": r["appearsinpayrollentry"],
                "appearsinledger":       r["appearsinledger"],
                "appearsinreports":      r["appearsinreports"],
                "requiresrate":          r["requiresrate"],
                "issystemstandard":      r["issystemstandard"],
                "iscustom":              bool(r["iscustom"]),
                "payitemstatus":         r["status"],
                "isactiveinperiod":      is_active_in_period,
                "sortorder":             r["sortorder"] if r["sortorder"] is not None else 0,
                "snapshoteffectivefrom": r["cfg_effectivefrom"],
                "sourceconfigid":        r["cfg_configid"],
            },
        )


def _period_end(frequency: str, start: date, interval_days: int | None) -> date:
    if frequency == "Week":
        return start + timedelta(days=6)
    if frequency == "Biweek":
        return start + timedelta(days=13)
    if frequency == "Month":
        return _month_end(start)
    if frequency == "Custom":
        if not interval_days or interval_days <= 0:
            raise ValueError("Custom frequency requires custom_interval_days > 0.")
        return start + timedelta(days=interval_days - 1)
    raise ValueError(f"Unknown payroll frequency: {frequency!r}")


# ---------------------------------------------------------------------------
# Helper: audit for CP-1C creation
# ---------------------------------------------------------------------------

async def _write_period_created_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    period_id: int,
    candidate_hash: str,
    mode: str,
    initial_status: str,
    start_date: date,
    end_date: date,
    setup_fp: str,
) -> None:
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'PERIOD_CREATED',
                 'payroll', 'PayrollPeriods', :eid,
                 NULL, :new_val, 'Period created via candidate key', 'Application')
        """),
        {
            "cid": company_id,
            "bid": branch_id,
            "uid": user_id,
            "eid": str(period_id),
            "new_val": json.dumps({
                "candidate_hash": candidate_hash,
                "mode": mode,
                "initial_status": initial_status,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "setup_fingerprint": setup_fp,
                "result": "CREATED",
            }),
        },
    )


# ---------------------------------------------------------------------------
# Service: GET /payroll/branches/{branch_id}/period-candidates
# ---------------------------------------------------------------------------

async def get_period_candidates(
    company_id: int,
    user_id: int,
    branch_id: int,
    mode: str,
    cursor_key: str | None,
    db: AsyncConnection,
) -> CandidatePreviewResponse:
    # Security checks
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to the requested branch.")

    await _check_permission(company_id, user_id, branch_id, "payroll.period.create", db)

    # Validate mode
    if mode not in ("OPEN_CREATION", "PREPARED_CREATION"):
        raise HTTPException(
            status_code=422,
            detail="mode must be OPEN_CREATION or PREPARED_CREATION.",
        )

    # Determine preview offset from cursor (if provided)
    preview_offset = 0
    if cursor_key:
        cursor_payload, _ = _decode_candidate_key(cursor_key)
        if cursor_payload.get("cid") != company_id:
            _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-company cursor rejected.")
        if cursor_payload.get("bid") != branch_id:
            _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-branch cursor rejected.")
        if cursor_payload.get("mode") != mode:
            _cp1c_error("INVALID_CANDIDATE_KEY", "Wrong mode in cursor.")
        preview_offset = int(cursor_payload.get("offset", 0))
    if preview_offset < 0 or preview_offset >= _CP1C_MAX_FUTURE:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Candidate navigation offset is out of range.")

    # Read branch status
    branch_row = (await db.execute(
        text(
            "SELECT b.status FROM core.branches b "
            "WHERE b.branchid = :bid AND b.companyid = :cid"
        ),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if branch_row is None or branch_row["status"] != "Active":
        _cp1c_error("BRANCH_INACTIVE", "Branch is inactive or not found.")

    # Read all non-Cancelled periods for slot/date computation
    period_rows = (await db.execute(
        text("""
            SELECT payrollperiodid, status, enddate
            FROM payroll.payrollperiods
            WHERE branchid = :bid AND companyid = :cid AND status != 'Cancelled'
            ORDER BY enddate DESC, payrollperiodid DESC
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().all()

    periods = [dict(r) for r in period_rows]
    pred_id = periods[0]["payrollperiodid"] if periods else None
    slot_fp = _slot_fingerprint(periods)

    # Slot matrix check
    creatable_base, blocked_reason = _check_slot_matrix(mode, periods)
    target_status = "Open" if mode == "OPEN_CREATION" else "Draft"

    first_start, _ = await _next_candidate_start(company_id, branch_id, db)

    async def signed_payload(offset: int) -> tuple[dict, Authority]:
        authority = await _candidate_at_offset(
            company_id, branch_id, first_start, offset, db,
        )
        setup_timeline, branch_timeline = await _timeline_fingerprints(
            company_id, branch_id, authority.setup_id, db,
        )
        return {
            "ver": _CP1C_VERSION,
            "purpose": _CP1C_PURPOSE,
            "cid": company_id,
            "bid": branch_id,
            "mode": mode,
            "target_status": target_status,
            "assignment_id": authority.assignment_id,
            "setup_id": authority.setup_id,
            "setup_version_id": authority.version_id,
            "config_hash": authority.config_hash,
            "setup_timeline": setup_timeline,
            "branch_timeline": branch_timeline,
            "start": authority.start_date.isoformat(),
            "end": authority.end_date.isoformat(),
            "slot_fp": slot_fp,
            "pred_id": pred_id,
            "offset": offset,
        }, authority

    payload, authority = await signed_payload(preview_offset)
    start_date, end_date = authority.start_date, authority.end_date
    freq = authority.schedule.frequency

    # Offset > 0 candidates are never directly creatable (navigation only)
    creatable = creatable_base and preview_offset == 0
    eff_blocked_reason = blocked_reason
    if preview_offset > 0 and eff_blocked_reason is None:
        eff_blocked_reason = "CANDIDATE_NOT_CURRENT"

    candidate_key, _ = _make_candidate_key(payload)
    label = _auto_period_name(freq, start_date, end_date)

    # Build navigation cursors
    prev_cursor: str | None = None
    next_cursor: str | None = None

    if preview_offset > 0:
        try:
            prev_payload, _ = await signed_payload(preview_offset - 1)
            prev_cursor, _ = _make_candidate_key(prev_payload)
        except HTTPException:
            pass

    if preview_offset < _CP1C_MAX_FUTURE - 1:
        try:
            next_payload, _ = await signed_payload(preview_offset + 1)
            next_cursor, _ = _make_candidate_key(next_payload)
        except HTTPException:
            pass

    return CandidatePreviewResponse(
        mode=mode,
        selected=CandidateSelectedInfo(
            candidate_key=candidate_key,
            target_status=target_status,
            start_date=start_date,
            end_date=end_date,
            period_type=freq,
            label=label,
            creatable=creatable,
            blocked_reason=eff_blocked_reason,
        ),
        navigation=CandidateNavigationInfo(
            previous_cursor=prev_cursor,
            next_cursor=next_cursor,
        ),
    )


# ---------------------------------------------------------------------------
# Service: POST /payroll/branches/{branch_id}/period-creations
# ---------------------------------------------------------------------------

async def create_period_from_candidate(
    company_id: int,
    user_id: int,
    branch_id: int,
    data: PeriodCreationRequest,
    db: AsyncConnection,
) -> PeriodCreationResponse:
    # Security checks
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to the requested branch.")

    await _check_permission(company_id, user_id, branch_id, "payroll.period.create", db)

    # Decode and verify candidate key
    payload, candidate_hash = _decode_candidate_key(data.candidate_key)

    if payload.get("cid") != company_id:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-company key rejected.")
    if payload.get("bid") != branch_id:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-branch key rejected.")

    mode = payload.get("mode", "")
    if mode not in ("OPEN_CREATION", "PREPARED_CREATION"):
        _cp1c_error("INVALID_CANDIDATE_KEY", "Unknown mode in candidate key.")

    claimed_offset = int(payload.get("offset", 0))
    if claimed_offset != 0:
        _cp1c_error("CANDIDATE_NOT_CURRENT", "Only offset-0 candidates may be created.")

    claimed_slot_fp = payload.get("slot_fp", "")
    claimed_start = date.fromisoformat(payload["start"])
    claimed_end = date.fromisoformat(payload["end"])
    target_status = payload["target_status"]
    if target_status != ("Open" if mode == "OPEN_CREATION" else "Draft"):
        _cp1c_error("INVALID_CANDIDATE_KEY", "Candidate mode and target status disagree.")

    # Acquire branch advisory lock (transaction-level)
    await _acquire_branch_workflow_lock(company_id, branch_id, db)

    # Replay check: if this hash already exists, return the existing period
    existing_row = (await db.execute(
        text("""
            SELECT payrollperiodid, status, periodcode, periodname,
                   periodtype, startdate, enddate, branchid
            FROM payroll.payrollperiods
            WHERE companyid = :cid AND branchid = :bid
              AND creationcandidatekeyhash = :hash
        """),
        {"cid": company_id, "bid": branch_id, "hash": candidate_hash},
    )).mappings().first()

    if existing_row is not None:
        if existing_row["status"] == "Cancelled":
            _cp1c_error(
                "CANDIDATE_ALREADY_CANCELLED",
                "The period created from this candidate was later cancelled. "
                "Generate a fresh candidate; this key cannot be replayed.",
            )
        return PeriodCreationResponse(
            result="ALREADY_EXISTS",
            payroll_period_id=existing_row["payrollperiodid"],
            branch_id=existing_row["branchid"],
            period_code=existing_row["periodcode"],
            period_name=existing_row["periodname"],
            period_type=existing_row["periodtype"],
            start_date=existing_row["startdate"],
            end_date=existing_row["enddate"],
            status=existing_row["status"],
        )

    # Re-read branch under lock
    branch_row = (await db.execute(
        text(
            "SELECT b.status FROM core.branches b "
            "WHERE b.branchid = :bid AND b.companyid = :cid"
        ),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if branch_row is None or branch_row["status"] != "Active":
        _cp1c_error("BRANCH_INACTIVE", "Branch is inactive or not found.")

    # Re-read periods under lock
    period_rows = (await db.execute(
        text("""
            SELECT payrollperiodid, status, startdate, enddate
            FROM payroll.payrollperiods
            WHERE branchid = :bid AND companyid = :cid AND status != 'Cancelled'
            ORDER BY enddate DESC, payrollperiodid DESC
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().all()

    periods = [dict(r) for r in period_rows]
    current_slot_fp = _slot_fingerprint(periods)

    if current_slot_fp != claimed_slot_fp:
        _cp1c_error("CANDIDATE_STALE", "Branch period slot state changed since this candidate was generated.")

    try:
        first_start, _ = await _next_candidate_start(company_id, branch_id, db)
        authority = await _resolve_candidate(company_id, branch_id, first_start, db)
    except HTTPException as exc:
        if exc.status_code == 409:
            _cp1c_error("CANDIDATE_STALE", "Payroll authority changed since preview.")
        raise
    setup_timeline, branch_timeline = await _timeline_fingerprints(
        company_id, branch_id, authority.setup_id, db,
    )
    if (payload.get("assignment_id") != authority.assignment_id
            or payload.get("setup_id") != authority.setup_id
            or payload.get("setup_version_id") != authority.version_id
            or payload.get("config_hash") != authority.config_hash
            or payload.get("setup_timeline") != setup_timeline
            or payload.get("branch_timeline") != branch_timeline
            or claimed_start != authority.start_date
            or claimed_end != authority.end_date):
        _cp1c_error("CANDIDATE_STALE", "Payroll authority, timeline, or dates changed since preview.")

    computed_start, computed_end = authority.start_date, authority.end_date
    freq = authority.schedule.frequency

    # Check slot matrix under lock
    creatable, slot_error = _check_slot_matrix(mode, periods)
    if not creatable:
        _cp1c_error(slot_error or "PERIOD_SLOT_CONFLICT", f"Cannot create period: {slot_error}.")

    # Date overlap check under lock
    overlap_row = (await db.execute(
        text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE branchid = :bid AND companyid = :cid
              AND status != 'Cancelled'
              AND startdate <= :end_date
              AND enddate >= :start_date
            LIMIT 1
        """),
        {"bid": branch_id, "cid": company_id,
         "start_date": computed_start, "end_date": computed_end},
    )).first()
    if overlap_row is not None:
        _cp1c_error("PERIOD_DATE_OVERLAP", "Date range overlaps an existing non-cancelled period.")

    # Fetch branch code for period code generation
    br_row = (await db.execute(
        text("SELECT branchcode FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    branch_code = br_row["branchcode"] if br_row else str(branch_id)

    period_name = _auto_period_name(freq, computed_start, computed_end)
    base_code = f"{branch_code}-{computed_start.strftime('%Y%m%d')}"
    period_code = await _unique_period_code(base_code, company_id, branch_id, db)

    setup_code = (await db.execute(text("""
        SELECT SetupCode FROM payroll.PayrollSetups
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
    """), {"cid": company_id, "sid": authority.setup_id})).scalar_one()

    # The Phase 1 FKs/trigger bind and validate this exact immutable authority.
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, periodcode, periodname, periodtype,
                 startdate, enddate, status, notes, createdbyuserid,
                 creationcandidatekeyhash, BranchPayrollSetupAssignmentID,
                 PayrollSetupVersionID, FrozenPayrollSetupID, FrozenPayrollSetupCode,
                 FrozenPayrollSetupVersionNumber, FrozenPayrollFrequency,
                 FrozenAnchorStartDate, FrozenCustomIntervalDays,
                 FrozenNormalDaysOffMask, ScheduleConfigHash)
            VALUES
                (:cid, :bid, :code, :name, :ptype,
                 :start, :end, :status, NULL, :uid,
                 :hash, :aid, :vid, :sid, :setup_code, :version_number,
                 :frequency, :anchor, :interval, :mask, :config_hash)
            RETURNING payrollperiodid
        """),
        {
            "cid":   company_id,
            "bid":   branch_id,
            "code":  period_code,
            "name":  period_name,
            "ptype": freq,
            "start": computed_start,
            "end":   computed_end,
            "status": target_status,
            "uid":   user_id,
            "hash":  candidate_hash,
            "aid": authority.assignment_id,
            "vid": authority.version_id,
            "sid": authority.setup_id,
            "setup_code": setup_code,
            "version_number": authority.version_number,
            "frequency": authority.schedule.frequency,
            "anchor": authority.schedule.anchor_start_date,
            "interval": authority.schedule.custom_interval_days,
            "mask": authority.schedule.normal_days_off_mask,
            "config_hash": authority.config_hash,
        },
    )
    new_period_id: int = insert_result.scalar_one()
    await initialize_period_audit_evidence_coverage(
        company_id=company_id, branch_id=branch_id, period_id=new_period_id, db=db,
    )

    # Write PERIOD_CREATED audit (exactly once — never on replay)
    await _write_period_created_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        period_id=new_period_id,
        candidate_hash=candidate_hash,
        mode=mode,
        initial_status=target_status,
        start_date=computed_start,
        end_date=computed_end,
        setup_fp=authority.config_hash,
    )
    await write_policy_audit(
        db, company_id=company_id, actor_user_id=user_id,
        event_type="PeriodCreated", payroll_setup_id=authority.setup_id,
        payroll_setup_version_id=authority.version_id,
        branch_payroll_setup_assignment_id=authority.assignment_id,
        branch_id=branch_id, effective_date=computed_start,
        new_config_hash=authority.config_hash,
        new_state={"period_id": new_period_id, "candidate_hash": candidate_hash,
                   "start": computed_start.isoformat(), "end": computed_end.isoformat()},
        affected_branch_ids=[branch_id],
        payroll_period_id=new_period_id,
    )

    await _create_period_day_rows(
        new_period_id, company_id, branch_id,
        authority.assignment_id, authority.version_id,
        computed_start, computed_end, authority.schedule.normal_days_off_mask, db,
    )

    # CP-2C: create period pay-item layout snapshot.
    await _create_period_pay_item_rows(
        new_period_id, company_id, branch_id, computed_start, db,
    )

    # CP-2E: create driver eligibility snapshot.
    # Open periods are frozen immediately; Draft (Prepared) periods are provisional.
    _is_open_creation = (target_status == "Open")
    await _create_period_driver_eligibility_rows(
        new_period_id, company_id, branch_id, db,
        snapshot_source="Generated",
        freeze=_is_open_creation,
        created_by_user_id=user_id,
        frozen_by_user_id=user_id if _is_open_creation else None,
    )

    created_at = datetime.now(UTC)

    return PeriodCreationResponse(
        result="CREATED",
        payroll_period_id=new_period_id,
        branch_id=branch_id,
        period_code=period_code,
        period_name=period_name,
        period_type=freq,
        start_date=computed_start,
        end_date=computed_end,
        status=target_status,
        created_at_utc=created_at,
    )


# ---------------------------------------------------------------------------
# Legacy period creation (compatibility entry point)
# ---------------------------------------------------------------------------
#
# Stage B4-21 moved create_period, compute_period_dates, and
# get_next_period_dates here from app.payroll.service — pure relocation, no
# behavior change. router.py's deprecated POST /payroll/periods now calls
# create_period directly; GET /payroll/periods/next-period-dates calls
# get_next_period_dates directly. Neither is unified with this module's
# candidate-based path (get_period_candidates / create_period_from_candidate)
# — see the module docstring for the specific behavioral differences that
# make them deliberately separate flows.

def compute_period_dates(
    frequency: str,
    anchor_start_date: date,
    last_end_date: date | None = None,
    custom_interval_days: int | None = None,
) -> tuple[date, date]:
    """
    Compute the next period's (start, end) dates from a branch's payroll setup.

    Rules
    -----
    - If *last_end_date* is None the first period starts on *anchor_start_date*.
    - Otherwise the next period starts the day after *last_end_date*.
    - Period length depends on *frequency*:

      ======= =============================================
      Week    7 days  (start + 6 days)
      Biweek  14 days (start + 13 days)
      Month   One calendar month (start to same day next month minus 1 day)
      Custom  Requires custom_interval_days > 0 (inclusive period length)
      ======= =============================================

    Both start and end are inclusive.

    Raises
    ------
    ValueError if *frequency* is unrecognised, or if 'Custom' and
    *custom_interval_days* is None or ≤ 0.
    """
    start = anchor_start_date if last_end_date is None else last_end_date + timedelta(days=1)

    if frequency == "Week":
        end = start + timedelta(days=6)
    elif frequency == "Biweek":
        end = start + timedelta(days=13)
    elif frequency == "Month":
        end = _month_end(start)
    elif frequency == "Custom":
        if not custom_interval_days or custom_interval_days <= 0:
            raise ValueError(
                "Custom frequency requires custom_interval_days > 0. "
                "Configure the custom cadence in Payroll Setup first."
            )
        end = start + timedelta(days=custom_interval_days - 1)
    else:
        raise ValueError(f"Unknown payroll frequency: {frequency!r}")

    return start, end


async def get_next_period_dates(
    company_id: int,
    user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> NextPeriodDates:
    """Disabled legacy next-date entry point; candidates own date authority."""
    _cp1c_error("LEGACY_NEXT_DATES_DISABLED", "Use the canonical period-candidates endpoint.", 410)
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to the requested branch.",
        )
    await _check_any_permission(
        company_id, user_id, branch_id,
        ["payroll.view", "payroll.entry", "payroll.finalize"],
        db,
    )

    # Fetch branch payroll setup
    setup_row = await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays
            FROM   payroll.branchpayrollsettings
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  isactive  = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )
    setup = setup_row.mappings().first()
    if setup is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No active payroll setup found for branch {branch_id}. "
                "Configure it in Settings → Payroll Setup first."
            ),
        )

    frequency: str         = setup["payrollfrequency"]
    anchor: date           = setup["anchorstartdate"]
    interval_days: int | None = setup.get("customintervaldays")

    # MAX end_date of non-cancelled periods for this branch
    last_row = await db.execute(
        text("""
            SELECT MAX(enddate) AS last_end
            FROM   payroll.payrollperiods
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  status    != 'Cancelled'
        """),
        {"bid": branch_id, "cid": company_id},
    )
    last_end: date | None = last_row.scalar_one_or_none()

    is_custom = (frequency == "Custom")
    start_date_out: date | None = None
    end_date_out:   date | None = None

    if frequency == "Custom":
        if interval_days and interval_days > 0:
            # Custom with a valid saved interval — compute automatically
            start_date_out, end_date_out = compute_period_dates(
                frequency, anchor, last_end, custom_interval_days=interval_days
            )
        # else: interval missing → leave start/end as None (setup incomplete)
    else:
        start_date_out, end_date_out = compute_period_dates(frequency, anchor, last_end)

    return NextPeriodDates(
        branch_id=branch_id,
        period_type=frequency,
        anchor_start_date=anchor,
        last_period_end_date=last_end,
        start_date=start_date_out,
        end_date=end_date_out,
        is_custom=is_custom,
        custom_interval_days=interval_days,
    )


async def create_period(
    company_id: int,
    user_id: int,
    data: PeriodCreate,
    db: AsyncConnection,
) -> PeriodSummary:
    """Disabled legacy direct-create entry point; candidate confirmation owns creation."""
    _cp1c_error("LEGACY_PERIOD_CREATION_DISABLED", "Use candidate-based period creation.", 410)
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    if not can_see_all and data.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to the target branch.",
        )

    # Permission gate: creating a period requires payroll.period.create.
    # This is intentionally separate from payroll.entry (data entry/editing).
    # Migration 0030 seeds this permission and assigns it to appropriate roles.
    await _check_permission(company_id, user_id, data.branch_id, "payroll.period.create", db)

    # CP-1C: Acquire branch advisory lock before overlap check and insert.
    # Serializes all period creation (both legacy and candidate-based) for this branch.
    await _acquire_branch_workflow_lock(company_id, data.branch_id, db)

    # CP-1D: Draft creation guard — legacy POST /payroll/periods creates a Draft;
    # this is only valid when exactly one Open exists and no Draft already exists.
    _slot_result = await db.execute(
        text("""
            SELECT status FROM payroll.payrollperiods
            WHERE  companyid = :cid AND branchid = :bid
              AND  status IN ('Draft', 'Open', 'InReview', 'Returned')
        """),
        {"cid": company_id, "bid": data.branch_id},
    )
    _slot_statuses = [r["status"] for r in _slot_result.mappings().all()]
    if "Draft" in _slot_statuses:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "DRAFT_SLOT_OCCUPIED",
                "message": (
                    "A Draft period already exists for this branch. "
                    "Only one Draft period is permitted per branch at a time."
                ),
            },
        )
    _open_count = _slot_statuses.count("Open")
    if _open_count == 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "DRAFT_CREATION_REQUIRES_OPEN",
                "message": (
                    "No Open period exists for this branch. "
                    "Legacy Draft creation requires exactly one Open period. "
                    "Use the candidate-based period creation endpoint instead."
                ),
            },
        )
    if _open_count > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "WORKFLOW_SLOT_CONFLICT",
                "message": (
                    "More than one Open period exists for this branch — "
                    "the workflow is in an inconsistent state."
                ),
            },
        )
    # Exactly one Open, no Draft → allow (InReview/Returned co-existence is valid)

    # Verify branch belongs to this company and get its BranchCode for period_code
    br_result = await db.execute(
        text(
            "SELECT branchcode FROM core.branches "
            "WHERE branchid = :bid AND companyid = :cid"
        ),
        {"bid": data.branch_id, "cid": company_id},
    )
    br_row = br_result.mappings().first()
    if br_row is None:
        raise HTTPException(
            status_code=422,
            detail="branch_id does not exist in this company.",
        )
    branch_code: str = br_row["branchcode"]

    # Guard: reject dates that overlap any existing non-cancelled period for this branch.
    # Every status except Cancelled reserves the date range — Locked and Archived
    # represent official historical records that must not be overlapped.
    overlap_row = await db.execute(
        text("""
            SELECT payrollperiodid, status, startdate, enddate
            FROM   payroll.payrollperiods
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  status    != 'Cancelled'
              AND  startdate <= :end_date
              AND  enddate   >= :start_date
            LIMIT  1
        """),
        {
            "bid":        data.branch_id,
            "cid":        company_id,
            "start_date": data.start_date,
            "end_date":   data.end_date,
        },
    )
    existing = overlap_row.mappings().first()
    if existing is not None:
        ex_id     = existing["payrollperiodid"]
        ex_status = existing["status"]
        ex_start  = existing["startdate"]
        ex_end    = existing["enddate"]
        raise HTTPException(
            status_code=422,
            detail=(
                f"A payroll period already exists for this date range. "
                f"Existing period (ID {ex_id}) status: {ex_status}, "
                f"dates: {ex_start} – {ex_end}. "
                f"Open the existing period instead of creating a new one. "
                f"Locked and Archived periods are official historical records and cannot be overlapped."
            ),
        )

    # Auto-generate period_name if not provided
    period_name = data.period_name or _auto_period_name(
        data.period_type, data.start_date, data.end_date
    )

    # Auto-generate a unique period_code
    base_code = f"{branch_code}-{data.start_date.strftime('%Y%m%d')}"
    period_code = await _unique_period_code(base_code, company_id, data.branch_id, db)

    # CP-2A: ensure schedule version and set on new period. Still under advisory lock.
    # None means no active setup — reject before inserting a period with NULL version.
    legacy_sv_id = await ensure_current_schedule_version(company_id, data.branch_id, user_id, db)
    if legacy_sv_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "PAYROLL_SETUP_REQUIRED",
                "message": (
                    "No active payroll setup or schedule version exists for this branch. "
                    "Configure payroll setup before creating periods."
                ),
            },
        )

    # Insert
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, periodcode, periodname, periodtype,
                 startdate, enddate, paydate, status, notes, createdbyuserid,
                 scheduleversionid)
            VALUES
                (:company_id, :branch_id, :period_code, :period_name, :period_type,
                 :start_date, :end_date, :pay_date, 'Draft', :notes, :created_by,
                 :sv_id)
            RETURNING payrollperiodid
        """),
        {
            "company_id":  company_id,
            "branch_id":   data.branch_id,
            "period_code": period_code,
            "period_name": period_name,
            "period_type": data.period_type,
            "start_date":  data.start_date,
            "end_date":    data.end_date,
            "pay_date":    data.pay_date,
            "notes":       data.notes,
            "created_by":  user_id,
            "sv_id":       legacy_sv_id,
        },
    )
    period_id: int = insert_result.scalar_one()
    await initialize_period_audit_evidence_coverage(
        company_id=company_id, branch_id=data.branch_id, period_id=period_id, db=db,
    )

    # CP-2B: create period-day snapshot from the schedule version's mask.
    # Read from PayrollScheduleVersions (immutable) — not from mutable BranchPayrollSettings.
    sv_mask_row = (await db.execute(
        text(
            "SELECT normaldaysoffmask FROM payroll.PayrollScheduleVersions "
            "WHERE scheduleversionid = :sv_id"
        ),
        {"sv_id": legacy_sv_id},
    )).mappings().first()
    period_mask = sv_mask_row["normaldaysoffmask"] if sv_mask_row else None
    await _create_period_day_rows(
        period_id, company_id, data.branch_id,
        legacy_sv_id, data.start_date, data.end_date, period_mask, db,
    )

    # CP-2C: create period pay-item layout snapshot.
    await _create_period_pay_item_rows(
        period_id, company_id, data.branch_id, data.start_date, db,
    )

    # CP-2E: create driver eligibility snapshot for legacy Draft periods.
    # Draft stays provisional (freeze=False); freeze happens when promoted to Open.
    await _create_period_driver_eligibility_rows(
        period_id, company_id, data.branch_id, db,
        snapshot_source="Generated",
        freeze=False,
        created_by_user_id=user_id,
    )

    return await get_period_by_id(company_id, user_id, period_id, db)
