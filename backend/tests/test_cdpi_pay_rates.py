"""
test_cdpi_pay_rates.py

PR-1B integration tests: CDPI PerUnit rate-slot triple.

Coverage:
  Part A -- Approval path (6 tests):
    Approve creates exactly 1 company-scoped RateType, PayItemRateTypeMap,
    PayItemRateSlots; PayItem has RequiresRate=TRUE; RateType code/fields correct.

  Part B -- Direct-create path (4 tests):
    create_direct_company_item creates the same triple; RequiresRate=TRUE;
    idempotency of ensure_cdpi_per_unit_rate_slot.

  Part C -- Pay Rates matrix filter (2 tests):
    Branch-inactive CDPI item excluded; branch-active included.

  Part D -- Backfill (2 tests):
    Migration 0047 repairs an existing CDPI PerUnit item without slots;
    re-running the repair is idempotent.
"""
import pytest
from sqlalchemy import text as _text

from app.cdpi.schemas import (
    CdpiDecideRequest,
    CdpiDecideAction,
    CdpiDirectCreateRequest,
)
from app.cdpi import service as cdpi_service
from app.pay_item_rate_slots import ensure_cdpi_per_unit_rate_slot


# ===========================================================================
# DB helpers (shared pattern with test_cdpi_approval.py)
# ===========================================================================

async def _get_ids(db):
    """Return (company_id, hq_branch_id, paytest_branch_id, admin_user_id)."""
    company_id = (await db.execute(
        _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO'")
    )).scalar_one()
    hq_id = (await db.execute(
        _text("SELECT branchid FROM core.branches WHERE branchcode = 'HQ'")
    )).scalar_one()
    paytest_id = (await db.execute(
        _text("SELECT branchid FROM core.branches WHERE branchcode = 'PAYTEST'")
    )).scalar_one()
    admin_id = (await db.execute(
        _text("SELECT userid FROM sec.users WHERE username = 'admin'")
    )).scalar_one()
    return company_id, hq_id, paytest_id, admin_id


async def _make_pending_request(db, *, company_id, branch_id, user_id,
                                 item_name="Bonus Loads"):
    from app.cdpi.schemas import CdpiRequestCreate, CdpiSubmitRequest
    draft = await cdpi_service.create_draft(
        company_id, user_id,
        CdpiRequestCreate(
            requesting_branch_id=branch_id,
            item_name=item_name,
            input_type="Number",
            calc_method_key="PerUnit",
            unit="trip",
        ),
        db,
    )
    return await cdpi_service.submit_draft(
        company_id, user_id, draft.request_id,
        CdpiSubmitRequest(expected_revision=1),
        db,
    )


async def _cleanup_cdpi_rate_rows(db, pay_item_id: int) -> None:
    await db.execute(
        _text("DELETE FROM payroll.payitemrateslots WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    rt_id = (await db.execute(
        _text("SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid LIMIT 1"),
        {"pid": pay_item_id},
    )).scalar_one_or_none()
    await db.execute(
        _text("DELETE FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    if rt_id is not None:
        await db.execute(
            _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid AND ratecode = :code"),
            {"rtid": rt_id, "code": f"CDPI_{pay_item_id}_PER_UNIT"},
        )


async def _cleanup_approved_request(db, *, request_id, pay_item_id: int) -> None:
    await db.execute(_text("ALTER TABLE payroll.cdpirequestevents DISABLE TRIGGER ALL"))
    try:
        await db.execute(
            _text("DELETE FROM payroll.cdpirequestevents WHERE requestid = :rid"),
            {"rid": str(request_id)},
        )
    finally:
        await db.execute(_text("ALTER TABLE payroll.cdpirequestevents ENABLE TRIGGER ALL"))
    await _cleanup_cdpi_rate_rows(db, pay_item_id)
    await db.execute(
        _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.cdpirequests WHERE requestid = :rid"),
        {"rid": str(request_id)},
    )
    await db.execute(
        _text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )


async def _cleanup_pay_item(db, *, pay_item_id: int) -> None:
    await _cleanup_cdpi_rate_rows(db, pay_item_id)
    await db.execute(
        _text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )


# ===========================================================================
# Part A — Approval path
# ===========================================================================

@pytest.mark.asyncio
class TestApprovalRateSlotTriple:

    async def test_approval_creates_one_rate_type(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(action=CdpiDecideAction.Approve,
                              expected_revision=req.revision, reason="OK"),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            rt = (await direct_db.execute(
                _text("""
                    SELECT rt.*
                    FROM   payroll.ratetypes rt
                    JOIN   payroll.payitemratetypemap m ON m.ratetypeid = rt.ratetypeid
                    WHERE  m.payitemid = :pid
                """),
                {"pid": pid},
            )).mappings().all()
            assert len(rt) == 1
            assert rt[0]["ratecode"] == f"CDPI_{pid}_PER_UNIT"
            assert rt[0]["companyid"] == cid
            assert rt[0]["isactive"] is True
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=result.approved_pay_item_id)

    async def test_approval_rate_type_is_company_scoped(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(action=CdpiDecideAction.Approve,
                              expected_revision=req.revision, reason="OK"),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            company_id_on_rt = (await direct_db.execute(
                _text("""
                    SELECT rt.companyid
                    FROM   payroll.ratetypes rt
                    WHERE  rt.ratecode = :code
                """),
                {"code": f"CDPI_{pid}_PER_UNIT"},
            )).scalar_one()
            assert company_id_on_rt == cid
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=result.approved_pay_item_id)

    async def test_approval_creates_one_map_row(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(action=CdpiDecideAction.Approve,
                              expected_revision=req.revision, reason="OK"),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            rows = (await direct_db.execute(
                _text("SELECT isprimary, status FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                {"pid": pid},
            )).mappings().all()
            assert len(rows) == 1
            assert rows[0]["isprimary"] is True
            assert rows[0]["status"] == "Active"
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=result.approved_pay_item_id)

    async def test_approval_creates_one_slot_row(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(action=CdpiDecideAction.Approve,
                              expected_revision=req.revision, reason="OK"),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            rows = (await direct_db.execute(
                _text("""
                    SELECT slotkey, slotrole, sourcekind, status
                    FROM   payroll.payitemrateslots
                    WHERE  payitemid = :pid
                """),
                {"pid": pid},
            )).mappings().all()
            assert len(rows) == 1
            assert rows[0]["slotkey"] == "per_unit_rate"
            assert rows[0]["slotrole"] == "per_unit"
            assert rows[0]["sourcekind"] == "CDPI"
            assert rows[0]["status"] == "Active"
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=result.approved_pay_item_id)

    async def test_approval_sets_requires_rate_true(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(action=CdpiDecideAction.Approve,
                              expected_revision=req.revision, reason="OK"),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            requiresrate = (await direct_db.execute(
                _text("SELECT requiresrate FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert requiresrate is True
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=result.approved_pay_item_id)

    async def test_approval_rate_slot_links_correct_rate_type(self, direct_db):
        """PayItemRateSlots.RateTypeID must equal PayItemRateTypeMap.RateTypeID."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(action=CdpiDecideAction.Approve,
                              expected_revision=req.revision, reason="OK"),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            map_rt = (await direct_db.execute(
                _text("SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            slot_rt = (await direct_db.execute(
                _text("SELECT ratetypeid FROM payroll.payitemrateslots WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert map_rt == slot_rt
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=result.approved_pay_item_id)


# ===========================================================================
# Part B — Direct-create path
# ===========================================================================

@pytest.mark.asyncio
class TestDirectCreateRateSlotTriple:

    async def test_direct_create_rate_type_has_correct_code(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="Night Bonus", input_type="Number",
                                    calc_method_key="PerUnit"),
            direct_db,
        )
        try:
            pid = result.pay_item_id
            code = (await direct_db.execute(
                _text("""
                    SELECT rt.ratecode
                    FROM   payroll.ratetypes rt
                    JOIN   payroll.payitemratetypemap m ON m.ratetypeid = rt.ratetypeid
                    WHERE  m.payitemid = :pid
                """),
                {"pid": pid},
            )).scalar_one()
            assert code == f"CDPI_{pid}_PER_UNIT"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_sets_requires_rate_true(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="Night Bonus", input_type="Number",
                                    calc_method_key="PerUnit"),
            direct_db,
        )
        try:
            requiresrate = (await direct_db.execute(
                _text("SELECT requiresrate FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": result.pay_item_id},
            )).scalar_one()
            assert requiresrate is True
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_slot_fields(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="Overtime", input_type="Time",
                                    calc_method_key="PerUnit", unit="Hour"),
            direct_db,
        )
        try:
            pid = result.pay_item_id
            slot = (await direct_db.execute(
                _text("""
                    SELECT slotkey, slotrole, sourcekind, isrequired
                    FROM   payroll.payitemrateslots
                    WHERE  payitemid = :pid AND status = 'Active'
                """),
                {"pid": pid},
            )).mappings().first()
            assert slot is not None
            assert slot["slotkey"] == "per_unit_rate"
            assert slot["slotrole"] == "per_unit"
            assert slot["sourcekind"] == "CDPI"
            assert slot["isrequired"] is True
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_ensure_cdpi_per_unit_rate_slot_is_idempotent(self, direct_db):
        """Calling ensure_cdpi_per_unit_rate_slot twice returns the same slot ID."""
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="Idempotency Check", input_type="Number",
                                    calc_method_key="PerUnit"),
            direct_db,
        )
        pid = result.pay_item_id
        try:
            slot_id_first = (await direct_db.execute(
                _text("SELECT payitemrateslotid FROM payroll.payitemrateslots WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            slot_id_second = await ensure_cdpi_per_unit_rate_slot(
                direct_db,
                pay_item_id=pid,
                item_name="Idempotency Check",
                input_type="Number",
                unit=None,
                company_id=cid,
            )
            assert slot_id_first == slot_id_second
            # Still exactly 1 slot row after second call.
            count = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitemrateslots WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert count == 1
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=pid)


# ===========================================================================
# Part C — Pay Rates matrix filter
# ===========================================================================

@pytest.mark.asyncio
class TestPayRatesMatrixFilter:

    async def test_branch_inactive_cdpi_item_excluded_from_matrix(self, direct_db):
        """
        CDPI item with IsDefaultBranchActive=FALSE and no BranchPayItemConfig for
        the branch must not appear in get_driver_rate_matrix for that branch.

        We test the COALESCE logic directly: COALESCE(NULL, FALSE) = FALSE filters
        out the item.  We create the item (direct-create, so no BranchPayItemConfig),
        verify no BPIC row exists for HQ, and assert the item is absent from the
        matrix query for the HQ branch.
        """
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="Matrix Exclude Test", input_type="Number",
                                    calc_method_key="PerUnit"),
            direct_db,
        )
        pid = result.pay_item_id
        try:
            # Confirm no BranchPayItemConfig for HQ.
            bpic_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*)
                    FROM   payroll.branchpayitemconfig
                    WHERE  payitemid = :pid AND branchid = :bid
                """),
                {"pid": pid, "bid": hq_id},
            )).scalar_one()
            assert bpic_count == 0

            # Evaluate the COALESCE filter: COALESCE(NULL, FALSE) = FALSE.
            filter_result = (await direct_db.execute(
                _text("""
                    SELECT COALESCE(bpic.isactive, pi.isdefaultbranchactive) AS passes_filter
                    FROM   payroll.payitems pi
                    LEFT JOIN payroll.branchpayitemconfig bpic
                           ON bpic.payitemid = pi.payitemid
                          AND bpic.branchid  = :bid
                    WHERE  pi.payitemid = :pid
                """),
                {"pid": pid, "bid": hq_id},
            )).scalar_one()
            assert filter_result is False
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=pid)

    async def test_branch_active_cdpi_item_included_in_matrix(self, direct_db):
        """
        CDPI item approved for a branch (BranchPayItemConfig.IsActive=TRUE) must
        appear in the matrix: COALESCE(TRUE, FALSE) = TRUE.
        """
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id, item_name="Matrix Include Test")
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(action=CdpiDecideAction.Approve,
                              expected_revision=req.revision, reason="OK"),
            direct_db,
        )
        pid = result.approved_pay_item_id
        try:
            # Approval creates BranchPayItemConfig for the requesting branch (HQ).
            filter_result = (await direct_db.execute(
                _text("""
                    SELECT COALESCE(bpic.isactive, pi.isdefaultbranchactive) AS passes_filter
                    FROM   payroll.payitems pi
                    LEFT JOIN payroll.branchpayitemconfig bpic
                           ON bpic.payitemid = pi.payitemid
                          AND bpic.branchid  = :bid
                    WHERE  pi.payitemid = :pid
                """),
                {"pid": pid, "bid": hq_id},
            )).scalar_one()
            assert filter_result is True
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=pid)


# ===========================================================================
# Part D — Backfill (migration 0047)
# ===========================================================================

@pytest.mark.asyncio
class TestCdpiPerUnitBackfill:

    async def _make_legacy_cdpi_item(self, db, *, company_id: int, branch_id: int,  # noqa: ARG002
                                      user_id: int, item_name: str) -> int:
        """
        Insert a CDPI PerUnit PayItem that simulates a pre-PR-1B item:
        RequiresRate=FALSE, no RateType, no PayItemRateTypeMap, no PayItemRateSlots.
        """
        import uuid as _uuid
        code = f"CDPI_LEGACY_{_uuid.uuid4().hex[:8].upper()}"
        pid = (await db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (payitemcode, payitemname, companyid, branchid,
                     category, datatype, itemscope, ratebehavior, status,
                     issystemstandard, isdefaultbranchactive, appearsinpayrollentry,
                     requiresrate, unit)
                VALUES
                    (:code, :name, :cid, NULL,
                     'Custom', 'Decimal', 'Daily', 'PerUnit', 'Active',
                     FALSE, FALSE, TRUE,
                     FALSE, 'trip')
                RETURNING payitemid
            """),
            {"code": code, "name": item_name, "cid": company_id},
        )).scalar_one()

        await db.execute(
            _text("""
                INSERT INTO payroll.cdpidefinitions
                    (payitemid, definitionschemaversion, lockedatutc, createdbyuserid)
                VALUES
                    (:pid, 1, NOW(), :uid)
            """),
            {"pid": pid, "uid": user_id},
        )
        return pid

    async def test_backfill_repairs_legacy_item(self, direct_db):
        """0047 backfill DO block creates the triple for a legacy CDPI PerUnit item."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        pid = await self._make_legacy_cdpi_item(
            direct_db, company_id=cid, branch_id=hq_id, user_id=admin_id,
            item_name="Legacy Bonus Loads",
        )
        try:
            # Run the backfill DO block directly.
            from pathlib import Path
            import sys
            migrations_dir = Path(__file__).parent.parent.parent / "migrations"
            if str(migrations_dir) not in sys.path:
                sys.path.insert(0, str(migrations_dir))
            from utils import statements_from_file
            sql_file = migrations_dir / "sql" / "0047_cdpi_perunit_rate_slot_backfill.sql"
            for stmt in statements_from_file(sql_file):
                await direct_db.execute(_text(stmt))

            # Verify the triple was created.
            map_count = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            slot_count = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitemrateslots WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            requiresrate = (await direct_db.execute(
                _text("SELECT requiresrate FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert map_count == 1
            assert slot_count == 1
            assert requiresrate is True
        finally:
            await _cleanup_cdpi_rate_rows(direct_db, pid)
            await direct_db.execute(
                _text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
                {"pid": pid},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": pid},
            )

    async def test_backfill_is_idempotent(self, direct_db):
        """Re-running the backfill DO block does not create duplicate rows."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        pid = await self._make_legacy_cdpi_item(
            direct_db, company_id=cid, branch_id=hq_id, user_id=admin_id,
            item_name="Idempotency Legacy",
        )
        try:
            from pathlib import Path
            import sys
            migrations_dir = Path(__file__).parent.parent.parent / "migrations"
            if str(migrations_dir) not in sys.path:
                sys.path.insert(0, str(migrations_dir))
            from utils import statements_from_file
            sql_file = migrations_dir / "sql" / "0047_cdpi_perunit_rate_slot_backfill.sql"
            stmts = list(statements_from_file(sql_file))

            # Run twice.
            for stmt in stmts:
                await direct_db.execute(_text(stmt))
            for stmt in stmts:
                await direct_db.execute(_text(stmt))

            map_count = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            slot_count = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitemrateslots WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert map_count == 1
            assert slot_count == 1
        finally:
            await _cleanup_cdpi_rate_rows(direct_db, pid)
            await direct_db.execute(
                _text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
                {"pid": pid},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": pid},
            )
