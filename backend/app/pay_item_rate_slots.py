"""
pay_item_rate_slots.py

Generic helpers for payroll.PayItemRateSlots.

PR-1A: Foundation only — this module is NOT imported by any router or service
in this release.  ensure_pay_item_rate_slot() is available for PR-1B to call
when CDPI approval or direct-create needs to register a PerUnit slot for a
newly created PayItem.

All functions use the same raw-SQL pattern as the rest of the codebase
(sqlalchemy text(), AsyncConnection) so there is no new ORM dependency and
no runtime schema-change risk from this module existing.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def ensure_pay_item_rate_slot(
    conn: AsyncConnection,
    *,
    pay_item_id: int,
    rate_type_id: int,
    slot_key: str,
    slot_role: str,
    sort_order: int = 1,
    is_required: bool = True,
    source_kind: str = "Seeded",
    created_by_user_id: int | None = None,
) -> int:
    """
    Ensure an Active PayItemRateSlots row exists for (pay_item_id, rate_type_id).

    Idempotent: if an Active row already exists for this (PayItemID, RateTypeID)
    pair, returns its PayItemRateSlotID without inserting a duplicate.  This
    matches the pattern used by the migration backfill and is safe to call
    inside an existing transaction.

    Parameters
    ----------
    conn
        An open AsyncConnection.  The caller is responsible for committing
        (or this runs inside the caller's transaction).
    pay_item_id
        FK to payroll.PayItems(PayItemID).
    rate_type_id
        FK to payroll.RateTypes(RateTypeID).
    slot_key
        Stable, non-empty business identifier for this slot.  Must not already
        be used by another Active slot for the same pay_item_id.
    slot_role
        Human-readable role label, e.g. 'perunit', 'legacy_primary'.
    sort_order
        Display/entry ordering within a pay item's slots.  Must be >= 1.
    is_required
        Whether this slot must always have a rate configured before the item
        can be used in payroll entry.
    source_kind
        Origin label for audit/introspection: 'Seeded', 'LegacyBackfill',
        'CDPI', 'Manual'.
    created_by_user_id
        Optional user ID to record in CreatedByUserID.

    Returns
    -------
    int
        PayItemRateSlotID of the existing or newly inserted row.

    Raises
    ------
    ValueError
        If slot_key is blank, slot_role is blank, or sort_order < 1.
    """
    if not slot_key.strip():
        raise ValueError("slot_key must be a non-empty string")
    if not slot_role.strip():
        raise ValueError("slot_role must be a non-empty string")
    if sort_order < 1:
        raise ValueError("sort_order must be a positive integer (>= 1)")

    # Return the existing active slot ID if one already exists.
    existing_id: int | None = (await conn.execute(
        text("""
            SELECT PayItemRateSlotID
            FROM   payroll.PayItemRateSlots
            WHERE  PayItemID  = :pay_item_id
              AND  RateTypeID = :rate_type_id
              AND  Status     = 'Active'
        """),
        {"pay_item_id": pay_item_id, "rate_type_id": rate_type_id},
    )).scalar_one_or_none()

    if existing_id is not None:
        return existing_id

    new_id: int = (await conn.execute(
        text("""
            INSERT INTO payroll.PayItemRateSlots
                (PayItemID, RateTypeID, SlotKey, SlotRole, SortOrder,
                 IsRequired, IsSystemGenerated, SourceKind, Status,
                 CreatedByUserID, CreatedAtUtc)
            VALUES
                (:pay_item_id, :rate_type_id, :slot_key, :slot_role, :sort_order,
                 :is_required, TRUE, :source_kind, 'Active',
                 :created_by_user_id, NOW())
            RETURNING PayItemRateSlotID
        """),
        {
            "pay_item_id":        pay_item_id,
            "rate_type_id":       rate_type_id,
            "slot_key":           slot_key.strip(),
            "slot_role":          slot_role.strip(),
            "sort_order":         sort_order,
            "is_required":        is_required,
            "source_kind":        source_kind,
            "created_by_user_id": created_by_user_id,
        },
    )).scalar_one()

    return new_id
