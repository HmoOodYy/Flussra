"""
Period Creation (CP-1C) — candidate-key generation/validation, schedule-version
policy, period-day and pay-item snapshot creation, and the two public
GET/POST candidate-based period-creation endpoints.

Extracted from app.payroll.service (Stages B4-3A, B4-4A) as a
dependency-closed leaf module — no behavior change, pure relocation.

B4-3A moved the minimal slot-matrix policy/core (_ACTIVE_SLOT_STATUSES,
_cp1c_error, _check_slot_matrix) first. B4-4A completed the write/generation
side of the domain: candidate-key/fingerprint/schedule-version helpers,
period-day-row and pay-item-row creation, the period-created audit, and the
two public endpoints (get_period_candidates, create_period_from_candidate).

_auto_period_name, _month_end, and _unique_period_code were not physically
part of the CP-1C block (they were defined near the legacy create_period/
compute_period_dates functions), but are genuinely shared period-naming/
date-math primitives used by both the legacy create_period path (which stays
in app.payroll.service) and this module's candidate-based path. They moved
here to avoid a period_creation.py -> service.py reverse dependency;
app.payroll.service now imports them back via a compatibility facade for
create_period's and compute_period_dates's continued internal use.

_check_slot_matrix is genuinely shared with Current Payroll Hub
(app.payroll.current_hub), which imports it directly from here.

_acquire_branch_workflow_lock was moved here too in the initial B4-4A pass,
but returned to app.payroll.service and then extracted into its own small
neutral module, app.payroll.workflow_lock, in a follow-up ownership
correction: it is a generic branch-level advisory-lock primitive with no
period-creation-specific logic, genuinely shared across five consumers
(this module's own create_period_from_candidate; legacy create_period,
change_period_status, resubmit_period, finalize_period in
app.payroll.service; and app.review.service) — none of which is more
entitled to own it than the others. This module now imports it directly
from app.payroll.workflow_lock, the same as its other consumers.

_validate_period_work_date, _period_has_pay_item_snapshot, and
_get_period_pay_item_snapshot were moved here in the initial B4-4A pass but
returned to app.payroll.service in a follow-up ownership correction: none of
them has any caller inside this module's own logic (Period Creation only
ever *writes* PayrollPeriodDays/PayrollPeriodPayItems once, via
_create_period_day_rows/_create_period_pay_item_rows above). All three are
read/validate accessors consumed exclusively by Draft-line CRUD, Period Pay
Lines, Day Grid, and app.payroll.off_drivers — domains not yet extracted —
so period_creation.py is not their correct owner merely because the tables
they read were first populated during period creation.
"""
import base64
import calendar as _calendar
import hmac as _hmac_mod
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import settings
from app.core.service import _check_branch_access, _check_permission, _require_not_driver_role
from app.payroll.audit_evidence import initialize_period_audit_evidence_coverage
from app.payroll.eligibility import _create_period_driver_eligibility_rows
from app.payroll.schemas import (
    CandidateNavigationInfo,
    CandidatePreviewResponse,
    CandidateSelectedInfo,
    PeriodCreationRequest,
    PeriodCreationResponse,
)
from app.payroll.workflow_lock import _acquire_branch_workflow_lock


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
# Used by both the legacy create_period path (app.payroll.service) and the
# candidate-based path below. Moved here (rather than importing them back
# from service.py) to keep this module dependency-closed against service.py.
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
    schedule_version_id: int,
    start_date: date,
    end_date: date,
    normal_days_off_mask: int | None,
    db: AsyncConnection,
) -> None:
    """
    Insert one PayrollPeriodDays row per calendar day for the given period.
    Called inside the period-creation transaction, still under the branch advisory lock.
    ON CONFLICT DO NOTHING ensures idempotency (candidate replay guard).
    Only called when schedule_version_id is not None.
    """
    day_rows = _generate_period_day_rows(start_date, end_date, normal_days_off_mask)
    if not day_rows:
        return

    values_sql = ", ".join(
        f"(:pid, :cid, :bid, :sv_id, :wd_{i}, :dow_{i}, :isd_{i}, :ico_{i})"
        for i in range(len(day_rows))
    )
    params: dict = {"pid": period_id, "cid": company_id, "bid": branch_id, "sv_id": schedule_version_id}
    for i, row in enumerate(day_rows):
        params[f"wd_{i}"]  = row["work_date"]
        params[f"dow_{i}"] = row["day_of_week"]
        params[f"isd_{i}"] = row["is_default_work_day"]
        params[f"ico_{i}"] = row["is_configured_off_day"]

    await db.execute(
        text(f"""
            INSERT INTO payroll.PayrollPeriodDays
                (PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID,
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

    # Read payroll setup — also fetch currentscheduleversionid for CP-2A binding.
    setup_row = (await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays,
                   currentscheduleversionid
            FROM payroll.branchpayrollsettings
            WHERE branchid = :bid AND companyid = :cid AND isactive = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if setup_row is None:
        _cp1c_error("PAYROLL_SETUP_REQUIRED", "No active payroll setup found for this branch.")

    freq = setup_row["payrollfrequency"]
    anchor = setup_row["anchorstartdate"]
    interval_days = setup_row.get("customintervaldays")
    if freq == "Custom" and (not interval_days or interval_days <= 0):
        _cp1c_error("PAYROLL_SETUP_INCOMPLETE", "Custom frequency requires custom_interval_days > 0.")

    setup_fp = _setup_fingerprint(freq, anchor, interval_days)
    # CP-2A: include the current schedule version ID in the signed payload so
    # any setup change (which creates a new version) invalidates this candidate.
    current_sv_id = setup_row.get("currentscheduleversionid")

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
    pred_end = periods[0]["enddate"] if periods else None
    pred_id = periods[0]["payrollperiodid"] if periods else None
    slot_fp = _slot_fingerprint(periods)

    # Slot matrix check
    creatable_base, blocked_reason = _check_slot_matrix(mode, periods)
    target_status = "Open" if mode == "OPEN_CREATION" else "Draft"

    # Compute dates at preview_offset
    try:
        start_date, end_date = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, preview_offset)
    except ValueError as e:
        _cp1c_error("PAYROLL_SETUP_INCOMPLETE", str(e))

    # Offset > 0 candidates are never directly creatable (navigation only)
    creatable = creatable_base and preview_offset == 0
    eff_blocked_reason = blocked_reason
    if preview_offset > 0 and eff_blocked_reason is None:
        eff_blocked_reason = "CANDIDATE_NOT_CURRENT"

    # Build candidate payload and sign it.
    # CP-2A: sv_id (schedule_version_id) is included so that any setup PUT
    # (which creates a new version row) invalidates candidates from the prior version.
    payload = {
        "ver": _CP1C_VERSION,
        "purpose": _CP1C_PURPOSE,
        "cid": company_id,
        "bid": branch_id,
        "mode": mode,
        "target_status": target_status,
        "freq": freq,
        "anchor": str(anchor),
        "interval": interval_days,
        "period_type": freq,
        "start": str(start_date),
        "end": str(end_date),
        "slot_fp": slot_fp,
        "setup_fp": setup_fp,
        "sv_id": current_sv_id,
        "pred_id": pred_id,
        "offset": preview_offset,
    }
    candidate_key, _ = _make_candidate_key(payload)
    label = _auto_period_name(freq, start_date, end_date)

    # Build navigation cursors
    prev_cursor: str | None = None
    next_cursor: str | None = None

    if preview_offset > 0:
        try:
            ps, pe = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, preview_offset - 1)
            prev_payload = {**payload, "offset": preview_offset - 1, "start": str(ps), "end": str(pe)}
            prev_cursor, _ = _make_candidate_key(prev_payload)
        except ValueError:
            pass

    if preview_offset < _CP1C_MAX_FUTURE - 1:
        try:
            ns, ne = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, preview_offset + 1)
            next_payload = {**payload, "offset": preview_offset + 1, "start": str(ns), "end": str(ne)}
            next_cursor, _ = _make_candidate_key(next_payload)
        except ValueError:
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

    claimed_setup_fp = payload.get("setup_fp", "")
    claimed_slot_fp = payload.get("slot_fp", "")
    claimed_start = date.fromisoformat(payload["start"])
    claimed_end = date.fromisoformat(payload["end"])
    target_status = payload["target_status"]
    # CP-2A: schedule version ID embedded in the candidate payload.
    # None means this candidate was generated before CP-2A was deployed.
    claimed_sv_id = payload.get("sv_id")

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

    # Re-read setup under lock — also fetch currentscheduleversionid for CP-2A validation.
    setup_row = (await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays,
                   currentscheduleversionid
            FROM payroll.branchpayrollsettings
            WHERE branchid = :bid AND companyid = :cid AND isactive = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if setup_row is None:
        _cp1c_error("PAYROLL_SETUP_REQUIRED", "No active payroll setup found.")

    freq = setup_row["payrollfrequency"]
    anchor = setup_row["anchorstartdate"]
    interval_days = setup_row.get("customintervaldays")
    current_setup_fp = _setup_fingerprint(freq, anchor, interval_days)

    if current_setup_fp != claimed_setup_fp:
        _cp1c_error("CANDIDATE_SETUP_CHANGED", "Payroll setup changed since this candidate was generated.")

    # CP-2A: schedule version validation.
    # If sv_id is absent the candidate is pre-CP-2A. Replay of an already-created
    # pre-CP-2A period is allowed (handled by the replay check above). New creation
    # from a no-sv_id candidate is rejected — clients must regenerate a fresh candidate.
    current_sv_id_from_settings = setup_row.get("currentscheduleversionid")
    if claimed_sv_id is None:
        _cp1c_error(
            "CANDIDATE_STALE",
            "Candidate lacks a schedule version ID (pre-CP-2A candidate). "
            "Regenerate a fresh candidate before creating a new period.",
        )
    if current_sv_id_from_settings != claimed_sv_id:
        _cp1c_error(
            "CANDIDATE_SETUP_CHANGED",
            "Payroll schedule version changed since this candidate was generated.",
        )

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

    # Recompute candidate dates and compare to claimed values
    pred_end = periods[0]["enddate"] if periods else None
    try:
        computed_start, computed_end = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, 0)
    except ValueError as e:
        _cp1c_error("PAYROLL_SETUP_INCOMPLETE", str(e))

    if computed_start != claimed_start or computed_end != claimed_end:
        _cp1c_error("CANDIDATE_STALE", "Candidate dates no longer match current branch state.")

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

    # CP-2A: ensure a valid schedule version exists and get its ID for the new period.
    # Still under the branch advisory lock. Uses repair path if needed (defensive).
    period_sv_id = await ensure_current_schedule_version(company_id, branch_id, user_id, db)

    # Insert period with candidate hash and schedule version
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, periodcode, periodname, periodtype,
                 startdate, enddate, status, notes, createdbyuserid,
                 creationcandidatekeyhash, scheduleversionid)
            VALUES
                (:cid, :bid, :code, :name, :ptype,
                 :start, :end, :status, NULL, :uid,
                 :hash, :sv_id)
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
            "sv_id": period_sv_id,
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
        setup_fp=current_setup_fp,
    )

    # CP-2B: create period-day snapshot from the schedule version's mask.
    # Read from PayrollScheduleVersions (immutable) — not from mutable BranchPayrollSettings.
    sv_mask_row = (await db.execute(
        text(
            "SELECT normaldaysoffmask FROM payroll.PayrollScheduleVersions "
            "WHERE scheduleversionid = :sv_id"
        ),
        {"sv_id": period_sv_id},
    )).mappings().first()
    period_mask = sv_mask_row["normaldaysoffmask"] if sv_mask_row else None
    await _create_period_day_rows(
        new_period_id, company_id, branch_id,
        period_sv_id, computed_start, computed_end, period_mask, db,
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

    created_at = datetime.now(timezone.utc)

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
