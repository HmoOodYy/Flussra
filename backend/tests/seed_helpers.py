"""
Shared DB-seeding helpers for current custom Daily PayItem fixtures.

Custom Daily PayItems are created through the CDPI workflow. These helpers
build the same canonical structure directly in the test DB (PayItems row +
CdpiDefinitions owner marker, optionally RateType / PayItemRateTypeMap) so
tests of Pay Rates, Day Grid, calculation and lifecycle can set up an item
without driving the whole CDPI request flow.
"""

from sqlalchemy import text


async def seed_cdpi_item(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    code: str,
    name: str = "Legacy Test Item",
    rate_behavior: str = "PerUnit",
    unit: str | None = "Stop",
    category: str = "Count",
    datatype: str = "Decimal",
) -> int:
    """Insert a company custom Daily PayItem owned by a CdpiDefinitions row. Returns pay_item_id."""
    result = await db_conn.execute(
        text("""
            INSERT INTO payroll.payitems (
                companyid, payitemcode, payitemname, category, datatype, unit,
                status, sortorder, appearsinpayrollentry, appearsinledger,
                appearsinreports, requiresrate, issystemstandard,
                itemscope, ratebehavior, isdefaultbranchactive,
                createdbyuserid
            ) VALUES (
                :cid, :code, :name, :category, :datatype, :unit,
                'Active', 100, TRUE, TRUE, TRUE, TRUE, FALSE,
                'Daily', :behavior, FALSE, :uid
            )
            RETURNING payitemid
        """),
        {
            "cid": company_id, "code": code, "name": name,
            "category": category, "datatype": datatype, "unit": unit,
            "behavior": rate_behavior, "uid": user_id,
        }
    )
    item_id = result.scalar_one()
    await db_conn.execute(
        text("""
            INSERT INTO payroll.cdpidefinitions
                (payitemid, definitionschemaversion, lockedatutc, createdbyuserid)
            VALUES (:piid, 1, NOW(), :uid)
        """),
        {"piid": item_id, "uid": user_id},
    )
    return item_id


async def seed_cdpi_item_with_rate_structure(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    code: str,
    name: str = "Legacy Test Item",
    unit: str = "Stop",
    category: str = "Count",
    rate_behavior: str = "PerUnit",
    rate_name: str | None = None,
) -> dict:
    """Insert a CDPI-owned Daily PayItem + RateType + PayItemRateTypeMap.
    Returns {pay_item_id, rate_type_id, rate_type_code}."""
    item_id = await seed_cdpi_item(
        db_conn, company_id=company_id, user_id=user_id,
        code=code, name=name, unit=unit, category=category,
        rate_behavior=rate_behavior,
    )

    effective_rate_name = rate_name or (name + " Rate")
    rate_code = f"CPI_{item_id}_1"

    rt_result = await db_conn.execute(
        text("""
            INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
            VALUES (:code, :rname, :unit, TRUE, :cid)
            ON CONFLICT (ratecode) DO UPDATE
                SET ratename = EXCLUDED.ratename, companyid = EXCLUDED.companyid
            RETURNING ratetypeid
        """),
        {"code": rate_code, "rname": effective_rate_name, "unit": unit or "Unit", "cid": company_id},
    )
    rt_id = rt_result.scalar_one()

    await db_conn.execute(
        text("""
            INSERT INTO payroll.payitemratetypemap
                (payitemid, ratetypeid, isprimary, status)
            VALUES (:piid, :rtid, TRUE, 'Active')
            ON CONFLICT (payitemid, ratetypeid) DO NOTHING
        """),
        {"piid": item_id, "rtid": rt_id},
    )
    await _ensure_rate_slot(db_conn, item_id=item_id, rate_type_id=rt_id, sort_order=1)

    return {"pay_item_id": item_id, "rate_type_id": rt_id, "rate_type_code": rate_code}


async def seed_cdpi_item_multi_rate(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    code: str,
    name: str = "Legacy Multi-Rate Item",
    unit: str = "Stop",
    category: str = "Count",
    rate_behavior: str = "RangeBracket",
    rate_names: list[str] | None = None,
) -> dict:
    """Insert a CDPI-owned Daily PayItem with multiple rate types.
    For OrdinalTier / RangeBracket / Block / RangeProgressive behaviors.
    Returns {pay_item_id, rate_type_ids: list[int], rate_type_codes: list[str]}."""
    item_id = await seed_cdpi_item(
        db_conn, company_id=company_id, user_id=user_id,
        code=code, name=name, unit=unit, category=category,
        rate_behavior=rate_behavior,
    )

    effective_names = rate_names or ["Rate 1", "Rate 2", "Rate 3"]
    rt_ids: list[int] = []
    rt_codes: list[str] = []

    for idx, rname in enumerate(effective_names, start=1):
        rate_code = f"CPI_{item_id}_{idx}"
        rt_result = await db_conn.execute(
            text("""
                INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
                VALUES (:code, :rname, :unit, TRUE, :cid)
                ON CONFLICT (ratecode) DO UPDATE
                    SET ratename = EXCLUDED.ratename, companyid = EXCLUDED.companyid
                RETURNING ratetypeid
            """),
            {"code": rate_code, "rname": rname, "unit": unit or "Unit", "cid": company_id},
        )
        rt_id = rt_result.scalar_one()
        await db_conn.execute(
            text("""
                INSERT INTO payroll.payitemratetypemap
                    (payitemid, ratetypeid, isprimary, status)
                VALUES (:piid, :rtid, :primary, 'Active')
                ON CONFLICT (payitemid, ratetypeid) DO NOTHING
            """),
            {"piid": item_id, "rtid": rt_id, "primary": (idx == 1)},
        )
        await _ensure_rate_slot(db_conn, item_id=item_id, rate_type_id=rt_id, sort_order=idx)
        rt_ids.append(rt_id)
        rt_codes.append(rate_code)

    return {"pay_item_id": item_id, "rate_type_ids": rt_ids, "rate_type_codes": rt_codes}


async def _ensure_rate_slot(db_conn, *, item_id: int, rate_type_id: int, sort_order: int) -> None:
    """The PayItemRateSlots row CDPI creation produces alongside each map row."""
    await db_conn.execute(
        text("""
            INSERT INTO payroll.payitemrateslots
                (payitemid, ratetypeid, slotkey, slotrole, sortorder, isrequired,
                 issystemgenerated, sourcekind, status)
            SELECT :piid, :rtid, :slotkey, 'perunit', :sort, TRUE, TRUE, 'CDPI', 'Active'
            WHERE NOT EXISTS (
                SELECT 1 FROM payroll.payitemrateslots
                WHERE payitemid = :piid AND ratetypeid = :rtid AND status = 'Active'
            )
        """),
        {"piid": item_id, "rtid": rate_type_id, "slotkey": f"rate_{rate_type_id}", "sort": sort_order},
    )


async def map_rate_type_to_item(
    db_conn,
    *,
    item_id: int,
    rate_type_id: int,
    is_primary: bool = True,
) -> None:
    """Create the PayItemRateTypeMap + PayItemRateSlots rows CDPI creation produces."""
    await db_conn.execute(
        text("""
            INSERT INTO payroll.payitemratetypemap
                (payitemid, ratetypeid, isprimary, status)
            VALUES (:piid, :rtid, :primary, 'Active')
            ON CONFLICT (payitemid, ratetypeid) DO NOTHING
        """),
        {"piid": item_id, "rtid": rate_type_id, "primary": is_primary},
    )
    await _ensure_rate_slot(db_conn, item_id=item_id, rate_type_id=rate_type_id, sort_order=1)


async def ensure_rate_slot(db_conn, *, item_id: int, rate_type_id: int, sort_order: int = 1) -> None:
    """Public form of the CDPI rate slot for an already mapped rate type."""
    await _ensure_rate_slot(db_conn, item_id=item_id, rate_type_id=rate_type_id, sort_order=sort_order)


async def attach_cdpi_owner(
    db_conn,
    *,
    item_id: int,
    rate_type_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """Give an existing company PayItem its canonical CDPI ownership.

    Inserts the CdpiDefinitions owner marker and, when the item's mapped rate type
    is given, the PayItemRateSlots row CDPI creation produces beside the map row.
    """
    await db_conn.execute(
        text("""
            INSERT INTO payroll.cdpidefinitions
                (payitemid, definitionschemaversion, lockedatutc, createdbyuserid)
            VALUES (:piid, 1, NOW(),
                    COALESCE(:uid, (SELECT MIN(userid) FROM sec.users)))
            ON CONFLICT (payitemid) DO NOTHING
        """),
        {"piid": item_id, "uid": user_id},
    )
    if rate_type_id is not None:
        await _ensure_rate_slot(db_conn, item_id=item_id, rate_type_id=rate_type_id, sort_order=1)


async def attach_cdpi_owner_by_code(db_conn, *, company_id: int, code: str) -> None:
    """attach_cdpi_owner for a company PayItem identified by its code."""
    item_id = (await db_conn.execute(
        text("SELECT payitemid FROM payroll.payitems WHERE companyid = :cid AND payitemcode = :code"),
        {"cid": company_id, "code": code},
    )).scalar_one()
    await attach_cdpi_owner(db_conn, item_id=item_id)
