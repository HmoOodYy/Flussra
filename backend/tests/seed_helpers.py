"""
Shared DB-seeding helpers for LLR-A compatibility tests.

These helpers insert legacy PayItems rows directly into the test DB,
bypassing the LLR-A HTTP guard that blocks new Custom Daily creation
through the legacy settings API.

Use these in any test that needs an existing legacy custom Daily item
to verify read / update / delete / rate-matrix compatibility behavior.
"""
from datetime import date as _date
from sqlalchemy import text


async def seed_legacy_item(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    code: str,
    name: str = "Legacy Test Item",
    item_scope: str = "Daily",
    rate_behavior: str = "PerUnit",
    unit: str | None = "Stop",
    category: str = "Count",
    datatype: str = "Decimal",
) -> int:
    """Insert a bare legacy PayItems row. Returns pay_item_id."""
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
                :scope, :behavior, FALSE, :uid
            )
            RETURNING payitemid
        """),
        {
            "cid": company_id, "code": code, "name": name,
            "category": category, "datatype": datatype, "unit": unit,
            "scope": item_scope, "behavior": rate_behavior, "uid": user_id,
        }
    )
    return result.scalar_one()


async def seed_legacy_item_with_rate_structure(
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
    """Insert a legacy Daily PayItem + CPI_ RateType + PayItemRateTypeMap.
    Mirrors what create_custom_pay_item() service did before LLR-A.
    Returns {pay_item_id, rate_type_id, rate_type_code}."""
    item_id = await seed_legacy_item(
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

    return {"pay_item_id": item_id, "rate_type_id": rt_id, "rate_type_code": rate_code}


async def seed_legacy_item_multi_rate(
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
    """Insert a legacy Daily PayItem with multiple CPI_ rate types.
    For OrdinalTier / RangeBracket / Block / RangeProgressive behaviors.
    Returns {pay_item_id, rate_type_ids: list[int], rate_type_codes: list[str]}."""
    item_id = await seed_legacy_item(
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
        rt_ids.append(rt_id)
        rt_codes.append(rate_code)

    return {"pay_item_id": item_id, "rate_type_ids": rt_ids, "rate_type_codes": rt_codes}


async def seed_legacy_request(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    branch_id: int,
    code: str,
    name: str = "Legacy Test Request",
    item_scope: str = "Daily",
    rate_behavior: str = "PerUnit",
    unit: str | None = "Unit",
    category: str = "Count",
    req_status: str = "PendingApproval",
) -> int:
    """Insert a legacy CustomPayItemRequests row. Returns request_id."""
    result = await db_conn.execute(
        text("""
            INSERT INTO payroll.custompayitemrequests (
                companyid, requestingbranchid, requestedbyuserid,
                payitemcode, payitemname, itemscope, ratebehavior,
                category, unit, sortorder, status
            ) VALUES (
                :cid, :bid, :uid,
                :code, :name, :scope, :behavior,
                :category, :unit, 100, :status
            )
            RETURNING requestid
        """),
        {
            "cid": company_id, "bid": branch_id, "uid": user_id,
            "code": code, "name": name, "scope": item_scope,
            "behavior": rate_behavior, "category": category,
            "unit": unit, "status": req_status,
        }
    )
    return result.scalar_one()


async def seed_legacy_approved_item(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    branch_id: int,
    code: str,
    name: str = "Legacy Approved Item",
    unit: str = "Stop",
) -> int:
    """Seed a legacy Daily item + BranchPayItemConfig (active, effective today)."""
    item_id = await seed_legacy_item(
        db_conn, company_id=company_id, user_id=user_id,
        code=code, name=name, unit=unit,
    )
    await db_conn.execute(
        text("""
            INSERT INTO payroll.branchpayitemconfig (
                companyid, branchid, payitemid, isactive, effectivefrom,
                createdbyuserid
            ) VALUES (
                :cid, :bid, :piid, TRUE, :eff, :uid
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "cid": company_id, "bid": branch_id, "piid": item_id,
            "eff": _date.today(), "uid": user_id,
        }
    )
    return item_id
