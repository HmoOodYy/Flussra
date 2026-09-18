"""
Canonical Bonus Event domain — CRUD, transactional batch apply, and the
zero-inclusive bonus summary.

Extracted from app.payroll.service (Stage B4-8) as a dependency-closed leaf
module — no behavior change, pure relocation.

Bonus canonical source is payroll.PayrollBonusEvents — distinct from the
generic Period Pay "BONUS" line type, which remains rejected wherever that
rule already applied before this move. apply_bonus_batch retains its bespoke
inline "SELECT ... FOR UPDATE" period lock (deliberately NOT
_lock_period_for_mutation): idempotent replay must still succeed on a period
that has since become Locked/Archived, which _lock_period_for_mutation's
editability check would reject.

_load_active_bonus_events and get_period_eligible_drivers are NOT part of
this module — both remain in app.payroll.service. _load_active_bonus_events
is consumed by Calculation, Reporting, and report_read_model.py, not by
Bonus CRUD/domain ownership. get_period_eligible_drivers is shared with
other period-pay behavior, not Bonus-exclusive.

Every private helper here (_get_bonus_event_by_id, _increment_bonus_data_revision,
_bonus_batch_canonical_payload, _bonus_batch_request_hash,
_get_bonus_events_in_order, _bonus_summary_driver_create_eligible) has zero
callers outside this module — no facade needed for any of them.

The six public functions (list_bonus_events, create_bonus_event,
update_bonus_event, void_bonus_event, apply_bonus_batch, get_bonus_summary)
have exactly one remaining caller each: router.py, which imports and calls
this module directly. No production or internal service.py caller remains
for any of them, so no service.py facade is kept for any Bonus symbol.
"""
import hashlib
import json
import uuid as _uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_any_permission,
    _check_permission,
    _has_any_permission,
    _require_not_driver_role,
)
from app.payroll.audit_evidence import capture_period_audit_evidence
from app.payroll.eligibility import (
    _assert_driver_eligible_for_period_via_snapshot,
    _driver_has_existing_period_pay_source,
    _period_has_driver_eligibility_snapshot,
)
from app.payroll.line_audit import _write_line_audit
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import (
    BonusBatchCreate,
    BonusBatchItem,
    BonusBatchResponse,
    BonusEventCreate,
    BonusEventResponse,
    BonusEventUpdate,
    BonusSummaryCapabilities,
    BonusSummaryDriver,
    BonusSummaryEvent,
    BonusSummaryResponse,
)

# ---------------------------------------------------------------------------
# CP-3A: Canonical Bonus Event CRUD
# ---------------------------------------------------------------------------

_BONUS_ENTRY_ALLOWED_STATUSES: set[str] = {"Open", "Returned"}


async def _get_bonus_event_by_id(
    bonus_event_id: int,
    company_id: int,
    db: AsyncConnection,
) -> BonusEventResponse:
    result = await db.execute(
        text("""
            SELECT
                be.payrollbonuseventid,
                be.payrollperiodid,
                be.companyid,
                be.branchid,
                be.driverid,
                be.amount,
                be.reason,
                be.notes,
                be.status,
                be.datarevision,
                be.sourcedraftlineid,
                be.voidedbyuserid,
                be.voidedatutc,
                be.voidreason,
                be.createdbyuserid,
                be.createdatutc,
                be.updatedbyuserid,
                be.updatedatutc
            FROM payroll.payrollbonusevents be
            WHERE be.payrollbonuseventid = :beid
              AND be.companyid           = :company_id
        """),
        {"beid": bonus_event_id, "company_id": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Bonus event not found.")
    return BonusEventResponse(
        bonus_event_id=int(row["payrollbonuseventid"]),
        period_id=int(row["payrollperiodid"]),
        company_id=int(row["companyid"]),
        branch_id=int(row["branchid"]),
        driver_id=int(row["driverid"]),
        amount=Decimal(str(row["amount"])),
        reason=row["reason"],
        notes=row["notes"],
        status=row["status"],
        data_revision=int(row["datarevision"]),
        source_draft_line_id=row["sourcedraftlineid"],
        voided_by_user_id=row["voidedbyuserid"],
        voided_at_utc=row["voidedatutc"],
        void_reason=row["voidreason"],
        created_by_user_id=row["createdbyuserid"],
        created_at_utc=row["createdatutc"],
        updated_by_user_id=row["updatedbyuserid"],
        updated_at_utc=row["updatedatutc"],
    )


async def list_bonus_events(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
) -> list[BonusEventResponse]:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    await _check_permission(company_id, user_id, period.branch_id, "payroll.view", db)

    params: dict = {"period_id": period_id, "company_id": company_id}
    driver_filter = ""
    if driver_id is not None:
        driver_filter = "AND be.driverid = :driver_id"
        params["driver_id"] = driver_id

    result = await db.execute(
        text(f"""
            SELECT
                be.payrollbonuseventid,
                be.payrollperiodid,
                be.companyid,
                be.branchid,
                be.driverid,
                be.amount,
                be.reason,
                be.notes,
                be.status,
                be.datarevision,
                be.sourcedraftlineid,
                be.voidedbyuserid,
                be.voidedatutc,
                be.voidreason,
                be.createdbyuserid,
                be.createdatutc,
                be.updatedbyuserid,
                be.updatedatutc
            FROM payroll.payrollbonusevents be
            WHERE be.payrollperiodid = :period_id
              AND be.companyid       = :company_id
              {driver_filter}
            ORDER BY be.driverid, be.payrollbonuseventid
        """),
        params,
    )
    rows = result.mappings().fetchall()
    return [
        BonusEventResponse(
            bonus_event_id=int(r["payrollbonuseventid"]),
            period_id=int(r["payrollperiodid"]),
            company_id=int(r["companyid"]),
            branch_id=int(r["branchid"]),
            driver_id=int(r["driverid"]),
            amount=Decimal(str(r["amount"])),
            reason=r["reason"],
            notes=r["notes"],
            status=r["status"],
            data_revision=int(r["datarevision"]),
            source_draft_line_id=r["sourcedraftlineid"],
            voided_by_user_id=r["voidedbyuserid"],
            voided_at_utc=r["voidedatutc"],
            void_reason=r["voidreason"],
            created_by_user_id=r["createdbyuserid"],
            created_at_utc=r["createdatutc"],
            updated_by_user_id=r["updatedbyuserid"],
            updated_at_utc=r["updatedatutc"],
        )
        for r in rows
    ]


async def _increment_bonus_data_revision(
    db: AsyncConnection,
    *,
    company_id: int,
    period_id: int,
    expected_bonus_data_revision: int | None = None,
) -> int:
    """
    Atomically increment payroll.PayrollPeriods.BonusDataRevision by 1 and
    return the new value (CP-3B2a).

    This is the period-level bonus-mutation concurrency token. Every
    successful bonus create/update/void increments it exactly once, in the
    same transaction as the event write. It is intentionally NOT derived
    from MAX(PayrollBonusEvents.DataRevision) — a voided or superseded
    event's per-row revision is not a reliable period-wide aggregate.

    With expected_bonus_data_revision supplied, the UPDATE is predicated on
    the current value matching it; zero rows means another writer already
    moved the revision, and this raises 409. This is the guard a future
    bonus batch (CP-3B2b) will use. Without it (today's single-event
    mutation paths), the UPDATE is unconditional and only fails to find a
    row if the period itself vanished mid-transaction — which cannot happen
    under the FOR UPDATE lock already held by every caller of this helper
    via _lock_period_for_mutation.
    """
    params: dict = {"pid": period_id, "cid": company_id}
    where_extra = ""
    if expected_bonus_data_revision is not None:
        where_extra = "AND bonusdatarevision = :expected"
        params["expected"] = expected_bonus_data_revision

    result = await db.execute(
        text(f"""
            UPDATE payroll.payrollperiods
            SET    bonusdatarevision = bonusdatarevision + 1
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              {where_extra}
            RETURNING bonusdatarevision
        """),
        params,
    )
    row = result.first()
    if row is None:
        if expected_bonus_data_revision is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bonus data has been modified by another request. "
                    f"Expected revision {expected_bonus_data_revision}. "
                    "Re-fetch and retry."
                ),
            )
        raise HTTPException(status_code=404, detail="Payroll period not found.")
    return int(row[0])


async def create_bonus_event(
    period_id: int,
    company_id: int,
    user_id: int,
    data: "BonusEventCreate",
    db: AsyncConnection,
) -> BonusEventResponse:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus events can only be added to Open or Returned periods. "
                f"Current status: '{period.status}'."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    # CP-3A: same snapshot-aware eligibility guard as non-BONUS period-pay.
    await _assert_driver_eligible_for_period_via_snapshot(
        company_id, period.branch_id, period_id, data.driver_id, db
    )

    await _lock_period_for_mutation(period_id, company_id, db)

    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollbonusevents
                (companyid, branchid, payrollperiodid, driverid,
                 amount, reason, notes, status,
                 createdbyuserid, createdatutc, datarevision)
            VALUES
                (:company_id, :branch_id, :period_id, :driver_id,
                 :amount, :reason, :notes, 'Active',
                 :user_id, NOW(), 1)
            RETURNING payrollbonuseventid
        """),
        {
            "company_id": company_id,
            "branch_id":  period.branch_id,
            "period_id":  period_id,
            "driver_id":  data.driver_id,
            "amount":     data.amount,
            "reason":     data.reason,
            "notes":      data.notes,
            "user_id":    user_id,
        },
    )
    new_id = insert_result.scalar_one()

    # CP-3B2a: one successful create = period BonusDataRevision +1, in the
    # same transaction as the insert. A later failure (e.g. audit write)
    # rolls this back along with the event insert.
    await _increment_bonus_data_revision(db, company_id=company_id, period_id=period_id)

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=new_id,
        action_code="BONUS_EVENT_ADDED",
        old_value=None,
        new_value={"driver_id": data.driver_id, "amount": str(data.amount)},
        entity_name="PayrollBonusEvents",
    )
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        domain="BONUS", action_code="BONUS_CREATED", source_entity_type="PayrollBonusEvents",
        source_entity_id=new_id, user_id=user_id, required_permission_code="payroll.entry", db=db,
        after_state={
            "driver_id": data.driver_id, "amount": data.amount, "reason": data.reason,
            "notes": data.notes, "status": "Active", "data_revision": 1,
        }, driver_id=data.driver_id, source_revision=1,
    )

    return await _get_bonus_event_by_id(new_id, company_id, db)


async def update_bonus_event(
    period_id: int,
    bonus_event_id: int,
    company_id: int,
    user_id: int,
    data: "BonusEventUpdate",
    db: AsyncConnection,
) -> BonusEventResponse:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus events can only be updated on Open or Returned periods. "
                f"Current status: '{period.status}'."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    event = await _get_bonus_event_by_id(bonus_event_id, company_id, db)
    if event.period_id != period_id:
        raise HTTPException(status_code=404, detail="Bonus event not found in this period.")
    if event.status == "Voided":
        raise HTTPException(status_code=422, detail="Cannot update a voided bonus event.")

    # Friendlier early 409 when the caller supplied a stale revision. This is
    # NOT the actual concurrency guard — a concurrent writer could still slip
    # in between this check and the UPDATE below. The atomic WHERE-clause
    # predicate on the UPDATE itself (CP-3B2a) is what actually enforces it.
    if data.data_revision is not None and data.data_revision != event.data_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bonus event has been modified by another request. "
                f"Expected revision {data.data_revision}, found {event.data_revision}. "
                "Re-fetch and retry."
            ),
        )

    # Build SET clause dynamically — only update provided fields.
    set_parts = ["updatedbyuserid = :user_id", "updatedatutc = NOW()",
                 "datarevision = datarevision + 1"]
    params: dict = {
        "beid": bonus_event_id,
        "company_id": company_id,
        "period_id": period_id,
        "branch_id": period.branch_id,
        "user_id": user_id,
    }
    old_snap: dict = {}
    new_snap: dict = {}

    if data.amount is not None:
        set_parts.append("amount = :amount")
        params["amount"] = data.amount
        old_snap["amount"] = str(event.amount)
        new_snap["amount"] = str(data.amount)
    if data.reason is not None:
        set_parts.append("reason = :reason")
        params["reason"] = data.reason
        old_snap["reason"] = event.reason
        new_snap["reason"] = data.reason
    if data.notes is not None:
        set_parts.append("notes = :notes")
        params["notes"] = data.notes
        old_snap["notes"] = event.notes
        new_snap["notes"] = data.notes

    if not (old_snap or new_snap):
        # No-op update — return current state unchanged.
        return event

    await _lock_period_for_mutation(period_id, company_id, db)

    # CP-3B2a: atomic predicate. Always scoped by period/company/branch AND
    # Status='Active' (a concurrent void between the checks above and this
    # UPDATE must not silently overwrite a now-voided event). When the caller
    # supplied data_revision, it is also part of the WHERE clause — this is
    # the real optimistic-concurrency guard, not the earlier pre-check.
    where_parts = [
        "payrollbonuseventid = :beid",
        "companyid           = :company_id",
        "payrollperiodid     = :period_id",
        "branchid            = :branch_id",
        "status              = 'Active'",
    ]
    if data.data_revision is not None:
        where_parts.append("datarevision = :expected_data_revision")
        params["expected_data_revision"] = data.data_revision

    set_clause = ", ".join(set_parts)
    where_clause = " AND ".join(where_parts)
    result = await db.execute(
        text(f"""
            UPDATE payroll.payrollbonusevents
            SET    {set_clause}
            WHERE  {where_clause}
            RETURNING payrollbonuseventid
        """),
        params,
    )
    if result.first() is None:
        # Stale revision or concurrently voided between the fetch above and
        # this UPDATE. Neither the event nor the period's BonusDataRevision
        # was changed by this request.
        raise HTTPException(
            status_code=409,
            detail=(
                "Bonus event has been modified by another request. "
                "Re-fetch and retry."
            ),
        )

    await _increment_bonus_data_revision(db, company_id=company_id, period_id=period_id)

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=bonus_event_id,
        action_code="BONUS_EVENT_UPDATED",
        old_value=old_snap,
        new_value=new_snap,
        entity_name="PayrollBonusEvents",
    )
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        domain="BONUS", action_code="BONUS_UPDATED", source_entity_type="PayrollBonusEvents",
        source_entity_id=bonus_event_id, user_id=user_id, required_permission_code="payroll.entry", db=db,
        before_state={
            "driver_id": event.driver_id, "amount": event.amount, "reason": event.reason,
            "notes": event.notes, "status": event.status, "data_revision": event.data_revision,
        },
        after_state={
            "driver_id": event.driver_id,
            "amount": data.amount if data.amount is not None else event.amount,
            "reason": data.reason if data.reason is not None else event.reason,
            "notes": data.notes if data.notes is not None else event.notes,
            "status": "Active", "data_revision": event.data_revision + 1,
        }, driver_id=event.driver_id, source_revision=event.data_revision + 1,
    )

    return await _get_bonus_event_by_id(bonus_event_id, company_id, db)


async def void_bonus_event(
    period_id: int,
    bonus_event_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BonusEventResponse:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus events can only be voided on Open or Returned periods. "
                f"Current status: '{period.status}'."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    event = await _get_bonus_event_by_id(bonus_event_id, company_id, db)
    if event.period_id != period_id:
        raise HTTPException(status_code=404, detail="Bonus event not found in this period.")

    # Idempotent: already voided → return as-is. No revision bump — this
    # request changed nothing.
    if event.status == "Voided":
        return event

    await _lock_period_for_mutation(period_id, company_id, db)

    # CP-3B2a: atomic predicate — Status='Active' in the WHERE clause means a
    # concurrent void between the check above and this UPDATE affects zero
    # rows here, which we then treat as the same idempotent case (re-fetch
    # and return the now-Voided state without bumping the revision again).
    result = await db.execute(
        text("""
            UPDATE payroll.payrollbonusevents
            SET    status          = 'Voided',
                   voidedbyuserid  = :user_id,
                   voidedatutc     = NOW(),
                   updatedbyuserid = :user_id,
                   updatedatutc    = NOW(),
                   datarevision    = datarevision + 1
            WHERE  payrollbonuseventid = :beid
              AND  companyid           = :company_id
              AND  payrollperiodid     = :period_id
              AND  branchid            = :branch_id
              AND  status              = 'Active'
            RETURNING payrollbonuseventid
        """),
        {
            "beid":       bonus_event_id,
            "company_id": company_id,
            "period_id":  period_id,
            "branch_id":  period.branch_id,
            "user_id":    user_id,
        },
    )
    if result.first() is None:
        # Concurrently voided between the fetch above and this UPDATE.
        return await _get_bonus_event_by_id(bonus_event_id, company_id, db)

    await _increment_bonus_data_revision(db, company_id=company_id, period_id=period_id)

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=bonus_event_id,
        action_code="BONUS_EVENT_VOIDED",
        old_value={"status": "Active"},
        new_value={"status": "Voided"},
        entity_name="PayrollBonusEvents",
    )
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        domain="BONUS", action_code="BONUS_VOIDED", source_entity_type="PayrollBonusEvents",
        source_entity_id=bonus_event_id, user_id=user_id, required_permission_code="payroll.entry", db=db,
        before_state={
            "driver_id": event.driver_id, "amount": event.amount, "reason": event.reason,
            "notes": event.notes, "status": event.status, "data_revision": event.data_revision,
        },
        after_state={"status": "Voided", "data_revision": event.data_revision + 1},
        driver_id=event.driver_id, source_revision=event.data_revision + 1,
    )

    return await _get_bonus_event_by_id(bonus_event_id, company_id, db)


# ---------------------------------------------------------------------------
# CP-3B2b: Create-only transactional bonus batch
# ---------------------------------------------------------------------------

def _bonus_batch_canonical_payload(
    expected_bonus_data_revision: int,
    items: list["BonusBatchItem"],
) -> dict:
    """Build the canonical request payload used both for hashing and durable
    storage.  Item order is preserved (significant); amounts are fixed
    two-decimal strings so numerically-equal inputs hash identically."""
    return {
        "version": 1,
        "expected_bonus_data_revision": expected_bonus_data_revision,
        "items": [
            {
                "driver_id": it.driver_id,
                "amount":    f"{it.amount:.2f}",
                "reason":    it.reason,
                "notes":     it.notes,
            }
            for it in items
        ],
    }


def _bonus_batch_request_hash(payload: dict) -> str:
    """SHA-256 hex of the canonical payload: UTF-8, keys sorted, compact
    separators.  Deterministic across equal requests; sensitive to item order
    (a reordered item list is a different request)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _get_bonus_events_in_order(
    ids: list[int],
    company_id: int,
    db: AsyncConnection,
) -> list[BonusEventResponse]:
    return [await _get_bonus_event_by_id(i, company_id, db) for i in ids]


async def apply_bonus_batch(
    period_id: int,
    company_id: int,
    user_id: int,
    data: "BonusBatchCreate",
    db: AsyncConnection,
) -> tuple[BonusBatchResponse, bool]:
    """
    Create-only transactional bonus batch (CP-3B2b).

    All-or-nothing: every item is validated before any event is inserted, and
    the whole request runs in one transaction (via get_db's engine.begin()), so
    any failure rolls back all events, the batch-request row, the revision bump,
    and every audit row.

    Idempotency: keyed on (Company, Branch, Period, IdempotencyKey). An exact
    replay (same key + same request hash) returns the stored result read-only —
    no writes, no revision bump, and it succeeds even if the period later became
    non-editable. A same-key/different-payload request is a 409.

    Returns (response, is_new): is_new=True for a fresh apply (HTTP 201),
    False for an idempotent replay (HTTP 200).
    """
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    branch_id = period.branch_id
    payload = _bonus_batch_canonical_payload(data.expected_bonus_data_revision, data.items)
    request_hash = _bonus_batch_request_hash(payload)

    # ── Lock the period row (does NOT enforce editability — replay must work on
    #    a now-locked/archived period). All idempotency and write decisions
    #    below happen under this lock, serializing concurrent batches and
    #    single-event mutations on the same period. ──────────────────────────── #
    locked = (await db.execute(
        text(
            "SELECT status FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid "
            "FOR UPDATE"
        ),
        {"pid": period_id, "cid": company_id},
    )).mappings().first()
    if locked is None:
        raise HTTPException(status_code=404, detail="Payroll period not found.")
    locked_status = locked["status"]

    # ── Idempotency lookup (under lock) ────────────────────────────────────── #
    existing = (await db.execute(
        text("""
            SELECT payrollbonusbatchrequestid, requesthash, batchcorrelationid,
                   idempotencykey, expectedbonusdatarevision, resultbonusdatarevision,
                   createdeventids, createdeventcount
            FROM   payroll.payrollbonusbatchrequests
            WHERE  companyid       = :cid
              AND  branchid        = :bid
              AND  payrollperiodid = :pid
              AND  idempotencykey  = :key
        """),
        {"cid": company_id, "bid": branch_id, "pid": period_id, "key": data.idempotency_key},
    )).mappings().first()

    if existing is not None:
        if existing["requesthash"] != request_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Idempotency key already used for a different bonus batch "
                    "payload (or a different expected revision) in this period."
                ),
            )
        # Exact replay — read-only, no status check, no writes, no bump.
        raw_ids = existing["createdeventids"]
        stored_ids = raw_ids if isinstance(raw_ids, list) else json.loads(raw_ids)
        events = await _get_bonus_events_in_order(list(stored_ids), company_id, db)
        response = BonusBatchResponse(
            period_id=period_id,
            branch_id=branch_id,
            batch_request_id=int(existing["payrollbonusbatchrequestid"]),
            idempotency_key=existing["idempotencykey"],
            batch_correlation_id=str(existing["batchcorrelationid"]),
            expected_bonus_data_revision=int(existing["expectedbonusdatarevision"]),
            result_bonus_data_revision=int(existing["resultbonusdatarevision"]),
            created_event_count=int(existing["createdeventcount"]),
            created_event_ids=[int(i) for i in stored_ids],
            events=events,
            replayed=True,
        )
        return response, False

    # ── New apply path ─────────────────────────────────────────────────────── #
    if locked_status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus batches can only be applied to Open or Returned periods. "
                f"Current status: '{locked_status}'."
            ),
        )

    # Validate every item before any insert (all-or-nothing). The eligibility
    # guard raises the same 422s as single-event POST /bonuses, including the
    # IncludedByExistingData-without-source and wrong-branch cases.
    for idx, item in enumerate(data.items):
        try:
            await _assert_driver_eligible_for_period_via_snapshot(
                company_id, branch_id, period_id, item.driver_id, db
            )
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=f"items[{idx}] (driver {item.driver_id}): {exc.detail}",
            )

    # Predicated revision bump — 409 if the caller's expected revision is stale.
    # Exactly one increment for the whole batch.
    result_revision = await _increment_bonus_data_revision(
        db,
        company_id=company_id,
        period_id=period_id,
        expected_bonus_data_revision=data.expected_bonus_data_revision,
    )

    batch_correlation_id = str(_uuid.uuid4())

    # Insert every event, stamped with the shared correlation id and the batch
    # idempotency key (non-unique on events — the authoritative unique record is
    # the PayrollBonusBatchRequests row).
    created_ids: list[int] = []
    total_amount = Decimal("0")
    for item in data.items:
        ins = await db.execute(
            text("""
                INSERT INTO payroll.payrollbonusevents
                    (companyid, branchid, payrollperiodid, driverid,
                     amount, reason, notes, status,
                     batchcorrelationid, idempotencykey,
                     createdbyuserid, createdatutc, datarevision)
                VALUES
                    (:cid, :bid, :pid, :did,
                     :amount, :reason, :notes, 'Active',
                     CAST(:corr AS UUID), :key,
                     :uid, NOW(), 1)
                RETURNING payrollbonuseventid
            """),
            {
                "cid":    company_id,
                "bid":    branch_id,
                "pid":    period_id,
                "did":    item.driver_id,
                "amount": item.amount,
                "reason": item.reason,
                "notes":  item.notes,
                "corr":   batch_correlation_id,
                "key":    data.idempotency_key,
                "uid":    user_id,
            },
        )
        created_ids.append(int(ins.scalar_one()))
        total_amount += item.amount

    # Insert the durable batch-request row. A concurrent same-key insert (should
    # be serialized by the period lock, but belt-and-suspenders) trips the unique
    # idempotency index — surface a clean 409 instead of a raw 500.
    try:
        batch_ins = await db.execute(
            text("""
                INSERT INTO payroll.payrollbonusbatchrequests
                    (companyid, branchid, payrollperiodid,
                     idempotencykey, requesthash, requestpayloadjson, batchcorrelationid,
                     expectedbonusdatarevision, resultbonusdatarevision,
                     createdeventids, createdeventcount, status,
                     createdbyuserid, createdatutc, appliedatutc)
                VALUES
                    (:cid, :bid, :pid,
                     :key, :hash, CAST(:payload AS JSONB), CAST(:corr AS UUID),
                     :expected, :result,
                     CAST(:event_ids AS JSONB), :event_count, 'Applied',
                     :uid, NOW(), NOW())
                RETURNING payrollbonusbatchrequestid
            """),
            {
                "cid":         company_id,
                "bid":         branch_id,
                "pid":         period_id,
                "key":         data.idempotency_key,
                "hash":        request_hash,
                "payload":     json.dumps(payload),
                "corr":        batch_correlation_id,
                "expected":    data.expected_bonus_data_revision,
                "result":      result_revision,
                "event_ids":   json.dumps(created_ids),
                "event_count": len(created_ids),
                "uid":         user_id,
            },
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=(
                "A bonus batch with this idempotency key is already being "
                "applied for this period. Retry to receive the applied result."
            ),
        )
    batch_request_id = int(batch_ins.scalar_one())

    # Per-event audit rows, all sharing the batch correlation id.
    for created_id, item in zip(created_ids, data.items):
        await _write_line_audit(
            db,
            company_id=company_id,
            branch_id=branch_id,
            user_id=user_id,
            line_id=created_id,
            action_code="BONUS_EVENT_ADDED",
            old_value=None,
            new_value={"driver_id": item.driver_id, "amount": str(item.amount)},
            entity_name="PayrollBonusEvents",
            correlation_id=batch_correlation_id,
        )
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="BONUS", action_code="BONUS_CREATED", source_entity_type="PayrollBonusEvents",
            source_entity_id=created_id, user_id=user_id, required_permission_code="payroll.entry",
            db=db,
            after_state={
                "driver_id": item.driver_id, "amount": item.amount, "reason": item.reason,
                "notes": item.notes, "status": "Active", "data_revision": 1,
            }, driver_id=item.driver_id, correlation_id=batch_correlation_id, source_revision=1,
        )

    # One batch-level audit row, same correlation id.
    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        line_id=batch_request_id,
        action_code="BONUS_BATCH_APPLIED",
        old_value=None,
        new_value={
            "idempotency_key":               data.idempotency_key,
            "request_hash":                  request_hash,
            "batch_correlation_id":          batch_correlation_id,
            "item_count":                    len(created_ids),
            "created_event_ids":             created_ids,
            "expected_bonus_data_revision":  data.expected_bonus_data_revision,
            "result_bonus_data_revision":    result_revision,
            "total_amount":                  str(total_amount),
        },
        entity_name="PayrollBonusBatchRequests",
        correlation_id=batch_correlation_id,
    )

    events = await _get_bonus_events_in_order(created_ids, company_id, db)
    response = BonusBatchResponse(
        period_id=period_id,
        branch_id=branch_id,
        batch_request_id=batch_request_id,
        idempotency_key=data.idempotency_key,
        batch_correlation_id=batch_correlation_id,
        expected_bonus_data_revision=data.expected_bonus_data_revision,
        result_bonus_data_revision=result_revision,
        created_event_count=len(created_ids),
        created_event_ids=created_ids,
        events=events,
        replayed=False,
    )
    return response, True


# ---------------------------------------------------------------------------
# CP-3B1: Zero-inclusive bonus summary
# ---------------------------------------------------------------------------

async def _bonus_summary_driver_create_eligible(
    eligibility_reason_code: str,
    period_id: int,
    driver_id: int,
    db: AsyncConnection,
) -> bool:
    """Read-only mirror of the snapshot branch of
    _assert_driver_eligible_for_period_via_snapshot — never raises, never
    mutates.  The bonus summary is only ever computed for periods that have
    a CP-2E snapshot (enforced earlier in get_bonus_summary), so only the
    snapshot branch of that eligibility check is relevant here.

    IncludedByExistingData drivers are period-eligible for VIEWING but not for
    NEW bonus creation unless they already have a period-pay source — this
    must match create_bonus_event's guard exactly so summary capabilities
    never claim can_create=true when POST /bonuses would reject.
    """
    if eligibility_reason_code == "IncludedByExistingData":
        return await _driver_has_existing_period_pay_source(period_id, driver_id, db)
    return True  # Active / TerminatedHistorical / Transferred


async def get_bonus_summary(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BonusSummaryResponse:
    """
    Zero-inclusive bonus summary for one period.

    Roster source is the CP-2E period eligibility snapshot ONLY — every
    snapshot-eligible driver appears even with zero bonus events, and bonus
    events for drivers not in the snapshot never expand the roster.  Periods
    without a CP-2E marker get a controlled 422 (no live-roster fallback).
    """
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # Draft (Prepared) periods have no financial exposure — same rule as the
    # period-eligible-drivers endpoint (CP-2F).
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Bonus summary is not available for Prepared (Draft) periods.",
        )

    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    if not await _period_has_driver_eligibility_snapshot(period_id, db):
        raise HTTPException(
            status_code=422,
            detail=(
                "BONUS_SUMMARY_UNAVAILABLE_NO_ELIGIBILITY_SNAPSHOT: "
                "the zero-inclusive bonus summary requires a period eligibility "
                "snapshot (CP-2E). This period has no snapshot marker, and the "
                "summary never falls back to the live driver roster."
            ),
        )

    # ── Roster: snapshot rows only ─────────────────────────────────────────── #
    roster_result = await db.execute(
        text("""
            SELECT ppde.driverid,
                   ppde.drivercodesnapshot,
                   ppde.drivernamesnapshot,
                   ppde.eligibilityreasoncode
            FROM   payroll.payrollperioddrivereligibility ppde
            WHERE  ppde.payrollperiodid     = :period_id
              AND  ppde.companyid           = :company_id
              AND  ppde.branchid            = :branch_id
              AND  ppde.iseligibleforperiod = TRUE
        """),
        {"period_id": period_id, "company_id": company_id, "branch_id": period.branch_id},
    )
    roster_rows = roster_result.mappings().fetchall()

    # ── Events: canonical PayrollBonusEvents only ──────────────────────────── #
    # BranchID is part of the filter (not just PeriodID/CompanyID) so a
    # same-company, cross-branch contaminated row can never contribute to a
    # snapshot driver's totals — PayrollBonusEvents.BranchID is denormalized
    # from the period at write time, so this filter is authoritative.
    events_result = await db.execute(
        text("""
            SELECT
                be.payrollbonuseventid,
                be.driverid,
                be.amount,
                be.reason,
                be.notes,
                be.status,
                be.datarevision,
                be.batchcorrelationid,
                be.idempotencykey,
                be.sourcedraftlineid,
                be.voidedbyuserid,
                be.voidedatutc,
                be.voidreason,
                be.createdbyuserid,
                be.createdatutc,
                be.updatedbyuserid,
                be.updatedatutc
            FROM payroll.payrollbonusevents be
            WHERE be.payrollperiodid = :period_id
              AND be.companyid       = :company_id
              AND be.branchid        = :branch_id
            ORDER BY be.driverid, be.payrollbonuseventid
        """),
        {"period_id": period_id, "company_id": company_id, "branch_id": period.branch_id},
    )
    events_by_driver: dict[int, list[BonusSummaryEvent]] = {}
    for r in events_result.mappings().fetchall():
        events_by_driver.setdefault(int(r["driverid"]), []).append(
            BonusSummaryEvent(
                bonus_event_id=int(r["payrollbonuseventid"]),
                driver_id=int(r["driverid"]),
                amount=Decimal(str(r["amount"])),
                reason=r["reason"],
                notes=r["notes"],
                status=r["status"],
                created_by_user_id=r["createdbyuserid"],
                created_at_utc=r["createdatutc"],
                updated_by_user_id=r["updatedbyuserid"],
                updated_at_utc=r["updatedatutc"],
                voided_by_user_id=r["voidedbyuserid"],
                voided_at_utc=r["voidedatutc"],
                void_reason=r["voidreason"],
                data_revision=int(r["datarevision"]),
                batch_correlation_id=(
                    str(r["batchcorrelationid"]) if r["batchcorrelationid"] is not None else None
                ),
                idempotency_key=r["idempotencykey"],
                source_draft_line_id=r["sourcedraftlineid"],
            )
        )

    # ── Period/user-wide mutation preconditions (shared by every driver) ──── #
    period_reason_codes: list[str] = []
    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        period_reason_codes.append("status_read_only")
    has_entry = await _has_any_permission(
        company_id, user_id, period.branch_id, ["payroll.entry"], db
    )
    if not has_entry:
        period_reason_codes.append("permission_entry_required")
    period_allows_mutation = not period_reason_codes

    # ── Aggregate events onto the snapshot roster, with per-driver capabilities #
    drivers: list[BonusSummaryDriver] = []
    for row in roster_rows:
        driver_id = int(row["driverid"])
        reason_code = row["eligibilityreasoncode"]
        events = events_by_driver.get(driver_id, [])
        active_events = [e for e in events if e.status == "Active"]

        reason_codes = list(period_reason_codes)
        can_create = period_allows_mutation
        if period_allows_mutation:
            driver_create_eligible = await _bonus_summary_driver_create_eligible(
                reason_code, period_id, driver_id, db
            )
            if not driver_create_eligible:
                can_create = False
                reason_codes.append("eligibility_existing_data_only")

        can_update_void = period_allows_mutation
        if period_allows_mutation and not active_events:
            can_update_void = False
            reason_codes.append("no_active_bonus_event")

        drivers.append(
            BonusSummaryDriver(
                driver_id=driver_id,
                driver_code=row["drivercodesnapshot"],
                driver_name=row["drivernamesnapshot"],
                eligibility_reason_code=reason_code,
                total_bonus=sum((e.amount for e in active_events), Decimal("0")),
                active_event_count=len(active_events),
                voided_event_count=len(events) - len(active_events),
                events=events,
                capabilities=BonusSummaryCapabilities(
                    can_create=can_create,
                    can_update=can_update_void,
                    can_void=can_update_void,
                    reason_codes=reason_codes,
                ),
            )
        )

    # Nonzero totals first (descending), then stable name/code/id order.
    drivers.sort(
        key=lambda d: (
            0 if d.total_bonus > 0 else 1,
            -d.total_bonus,
            d.driver_name or "",
            d.driver_code or "",
            d.driver_id,
        )
    )

    # CP-3B2a: bonus_data_revision comes straight from PayrollPeriods, never
    # from MAX(PayrollBonusEvents.DataRevision).
    revision_row = (await db.execute(
        text(
            "SELECT bonusdatarevision FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid"
        ),
        {"pid": period_id, "cid": company_id},
    )).first()
    bonus_data_revision = int(revision_row[0]) if revision_row is not None else 0

    return BonusSummaryResponse(
        period_id=period_id,
        branch_id=period.branch_id,
        period_status=period.status,
        eligibility_source="PeriodEligibilitySnapshot",
        drivers=drivers,
        active_event_count=sum(d.active_event_count for d in drivers),
        active_bonus_total=sum((d.total_bonus for d in drivers), Decimal("0")),
        bonus_data_revision=bonus_data_revision,
    )
