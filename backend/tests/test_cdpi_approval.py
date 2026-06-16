"""
test_cdpi_approval.py

Task 6 integration tests: Approval + Direct Company Creation.

Coverage:
  Part A -- Approval (17 tests):
    Approve action transitions request to Approved, creates PayItems,
    CdpiDefinitions, BranchPayItemConfig for requesting branch, sets
    ApprovedPayItemID, inserts Approved event.

  Part B -- Direct Company Creation (12 tests):
    POST /settings/cdpi/direct-company-items creates PayItems + CdpiDefinitions
    with SourceRequestID=NULL, no BranchPayItemConfig, no CdpiRequest.

  Part C -- Regression (4 tests):
    Existing ReturnToDraft/Reject/submit/copy paths still work with the
    extended CdpiDecideAction enum.
"""
import uuid
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text as _text

from app.cdpi.schemas import (
    CdpiRequestCreate,
    CdpiSubmitRequest,
    CdpiDecideRequest,
    CdpiDirectCreateRequest,
    CdpiDecideAction,
)
from app.cdpi import service as cdpi_service


# ===========================================================================
# Shared DB helpers (mirrors test_cdpi_draft.py pattern)
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


async def _create_test_user(db, *, company_id: int, username: str) -> int:
    return (await db.execute(
        _text("""
            INSERT INTO sec.users
                (companyid, username, displayname, passwordhash, isactive, canlogin)
            VALUES (:cid, :uname, :uname, 'x', TRUE, TRUE)
            RETURNING userid
        """),
        {"cid": company_id, "uname": username},
    )).scalar_one()


async def _create_company_role(db, *, company_id: int, role_code: str,
                                perms: list) -> int:
    role_id = (await db.execute(
        _text("""
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel,
                 isdefault, isprotected, iscustom, isactive)
            VALUES (:cid, :rcode, :rcode, 50, FALSE, FALSE, TRUE, TRUE)
            RETURNING companyroleid
        """),
        {"cid": company_id, "rcode": role_code},
    )).scalar_one()
    for perm in perms:
        await db.execute(
            _text("""
                INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
                VALUES (:rid, :perm)
                ON CONFLICT DO NOTHING
            """),
            {"rid": role_id, "perm": perm},
        )
    return role_id


async def _assign_role(db, *, user_id: int, company_id: int, role_id: int,
                        scope: str = "AllCompanyBranches",
                        branch_id=None) -> None:
    await db.execute(
        _text("""
            INSERT INTO sec.userbranchroles
                (userid, companyid, branchid, companyroleId, scopetype, isactive)
            VALUES (:uid, :cid, :bid, :rid, :scope, TRUE)
        """),
        {"uid": user_id, "cid": company_id, "bid": branch_id,
         "rid": role_id, "scope": scope},
    )


async def _cleanup_user(db, user_id: int) -> None:
    await db.execute(
        _text("DELETE FROM sec.userbranchroles WHERE userid = :uid"), {"uid": user_id}
    )
    await db.execute(
        _text("DELETE FROM sec.users WHERE userid = :uid"), {"uid": user_id}
    )


async def _cleanup_role(db, role_id: int) -> None:
    await db.execute(
        _text("DELETE FROM sec.companyrolepermissions WHERE companyroleid = :rid"),
        {"rid": role_id},
    )
    await db.execute(
        _text("DELETE FROM sec.companyroles WHERE companyroleid = :rid"), {"rid": role_id}
    )


async def _cleanup_requests(db, *request_ids) -> None:
    """Delete CDPI request events then the requests themselves."""
    for rid in request_ids:
        await db.execute(
            _text("ALTER TABLE payroll.cdpirequestevents DISABLE TRIGGER ALL")
        )
        try:
            await db.execute(
                _text("DELETE FROM payroll.cdpirequestevents WHERE requestid = :rid"),
                {"rid": str(rid)},
            )
        finally:
            await db.execute(
                _text("ALTER TABLE payroll.cdpirequestevents ENABLE TRIGGER ALL")
            )
        await db.execute(
            _text("DELETE FROM payroll.cdpirequests WHERE requestid = :rid"),
            {"rid": str(rid)},
        )


async def _cleanup_approved_request(db, *, request_id, pay_item_id: int) -> None:
    """
    Remove an approved CDPI request and its associated PayItem.

    Deletion order satisfies FK and check constraints:
      1. CdpiRequestEvents (immutability trigger bypassed)
      2. BranchPayItemConfig (FK -> PayItems)
      3. CdpiDefinitions (FK -> PayItems)
      4. CdpiRequests (FK -> PayItems via ApprovedPayItemID; check requires both or neither)
      5. PayItems

    We delete CdpiRequests before PayItems to avoid nulling ApprovedPayItemID while
    Status='Approved' (the check constraint forbids that intermediate state).
    """
    await db.execute(
        _text("ALTER TABLE payroll.cdpirequestevents DISABLE TRIGGER ALL")
    )
    try:
        await db.execute(
            _text("DELETE FROM payroll.cdpirequestevents WHERE requestid = :rid"),
            {"rid": str(request_id)},
        )
    finally:
        await db.execute(
            _text("ALTER TABLE payroll.cdpirequestevents ENABLE TRIGGER ALL")
        )
    await db.execute(
        _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    # Delete the request row (holds FK to PayItems) before deleting PayItems.
    await db.execute(
        _text("DELETE FROM payroll.cdpirequests WHERE requestid = :rid"),
        {"rid": str(request_id)},
    )
    await db.execute(
        _text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )


async def _cleanup_pay_item(db, *, pay_item_id: int) -> None:
    """Remove a directly-created PayItem and its CdpiDefinition row."""
    await db.execute(
        _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )


async def _make_pending_request(db, *, company_id, branch_id, user_id,
                                 item_name="Miles", input_type="Number"):
    """Create a Draft then submit it to reach PendingCompanyApproval."""
    draft = await cdpi_service.create_draft(
        company_id, user_id,
        CdpiRequestCreate(
            requesting_branch_id=branch_id,
            item_name=item_name,
            input_type=input_type,
            calc_method_key="PerUnit",
        ),
        db,
    )
    pending = await cdpi_service.submit_draft(
        company_id, user_id, draft.request_id,
        CdpiSubmitRequest(expected_revision=1),
        db,
    )
    return pending


# ===========================================================================
# Part A -- Approval (17 tests)
# ===========================================================================

@pytest.mark.asyncio
class TestApproveHappyPath:

    async def test_approve_transitions_status_to_approved(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        try:
            result = await cdpi_service.decide_request(
                cid, admin_id, req.request_id,
                CdpiDecideRequest(
                    action=CdpiDecideAction.Approve,
                    expected_revision=req.revision,
                    reason="Looks good",
                ),
                direct_db,
            )
            assert result.status == "Approved"
        finally:
            if result.approved_pay_item_id:
                await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)
            else:
                await _cleanup_requests(direct_db, req.request_id)

    async def test_approve_sets_approved_pay_item_id(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            assert result.approved_pay_item_id is not None
            assert isinstance(result.approved_pay_item_id, int)
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)

    async def test_approve_increments_revision(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            assert result.revision == req.revision + 1
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)

    async def test_approve_creates_pay_item_row(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id, item_name="Loads")
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            row = (await direct_db.execute(
                _text("SELECT * FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": pid},
            )).mappings().first()
            assert row is not None
            assert row["payitemname"] == "Loads"
            assert row["companyid"] == cid
            assert row["branchid"] is None
            assert row["category"] == "Custom"
            assert row["datatype"] == "Decimal"
            assert row["itemscope"] == "Daily"
            assert row["ratebehavior"] == "PerUnit"
            assert row["status"] == "Active"
            assert row["issystemstandard"] is False
            assert row["isdefaultbranchactive"] is False
            assert row["appearsinpayrollentry"] is True
            assert row["requestingbranchid"] == hq_id
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)

    async def test_approve_pay_item_code_has_cdpi_prefix(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            code = (await direct_db.execute(
                _text("SELECT payitemcode FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert code.startswith("CDPI")
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)

    async def test_approve_creates_cdpi_definition(self, direct_db):
        """Approval must create a CdpiDefinitions row linked by ApprovedPayItemID."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            defn = (await direct_db.execute(
                _text("SELECT * FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
                {"pid": pid},
            )).mappings().first()
            assert defn is not None
            assert defn["definitionschemaversion"] == 1
            assert defn["lockedatutc"] is not None
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)

    async def test_approve_creates_branch_pay_item_config_for_requesting_branch(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            cfg = (await direct_db.execute(
                _text("""
                    SELECT * FROM payroll.branchpayitemconfig
                    WHERE payitemid = :pid
                """),
                {"pid": pid},
            )).mappings().all()
            assert len(cfg) == 1
            assert cfg[0]["branchid"] == hq_id
            assert cfg[0]["isactive"] is True
            assert cfg[0]["companyid"] == cid
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)

    async def test_approve_creates_no_rate_rows(self, direct_db):
        """No PayItemRateTypeMap rows must be created (RateTypes are not CDPI's concern)."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            map_count = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert map_count == 0
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                            pay_item_id=result.approved_pay_item_id)

    async def test_approve_inserts_approved_event(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="Approved by admin",
            ),
            direct_db,
        )
        try:
            events = (await direct_db.execute(
                _text("""
                    SELECT eventtype, fromstatus, tostatus, reason
                    FROM payroll.cdpirequestevents
                    WHERE requestid = :rid AND eventtype = 'Approved'
                """),
                {"rid": str(req.request_id)},
            )).mappings().all()
            assert len(events) == 1
            ev = events[0]
            assert ev["fromstatus"] == "PendingCompanyApproval"
            assert ev["tostatus"] == "Approved"
            assert ev["reason"] == "Approved by admin"
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)


@pytest.mark.asyncio
class TestApproveGuards:

    async def test_approve_requires_company_scope(self, direct_db):
        """A SpecificBranch user with payitems.edit must get 403."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        branch_uid = await _create_test_user(direct_db, company_id=cid,
                                              username=f"approver_branch_{uuid.uuid4().hex[:6]}")
        role_id = await _create_company_role(direct_db, company_id=cid,
                                              role_code=f"APPR_BR_{uuid.uuid4().hex[:6]}",
                                              perms=["payitems.edit"])
        await _assign_role(direct_db, user_id=branch_uid, company_id=cid,
                           role_id=role_id, scope="SpecificBranch", branch_id=hq_id)
        try:
            with pytest.raises(HTTPException) as exc:
                await cdpi_service.decide_request(
                    cid, branch_uid, req.request_id,
                    CdpiDecideRequest(
                        action=CdpiDecideAction.Approve,
                        expected_revision=req.revision,
                        reason="Should fail",
                    ),
                    direct_db,
                )
            assert exc.value.status_code == 403
        finally:
            await _cleanup_user(direct_db, branch_uid)
            await _cleanup_role(direct_db, role_id)
            await _cleanup_requests(direct_db, req.request_id)

    async def test_approve_wrong_company_returns_404(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        try:
            with pytest.raises(HTTPException) as exc:
                await cdpi_service.decide_request(
                    cid + 9999, admin_id, req.request_id,
                    CdpiDecideRequest(
                        action=CdpiDecideAction.Approve,
                        expected_revision=req.revision,
                        reason="Should fail",
                    ),
                    direct_db,
                )
            assert exc.value.status_code == 404
        finally:
            await _cleanup_requests(direct_db, req.request_id)

    async def test_approve_nonexistent_request_returns_404(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc:
            await cdpi_service.decide_request(
                cid, admin_id, uuid.uuid4(),
                CdpiDecideRequest(
                    action=CdpiDecideAction.Approve,
                    expected_revision=2,
                    reason="Should fail",
                ),
                direct_db,
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestApproveStatusAndConcurrency:

    async def test_approve_draft_returns_422(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await cdpi_service.create_draft(
            cid, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Miles",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc:
                await cdpi_service.decide_request(
                    cid, admin_id, draft.request_id,
                    CdpiDecideRequest(
                        action=CdpiDecideAction.Approve,
                        expected_revision=draft.revision,
                        reason="Too early",
                    ),
                    direct_db,
                )
            assert exc.value.status_code == 422
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_approve_stale_revision_returns_409(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        try:
            with pytest.raises(HTTPException) as exc:
                await cdpi_service.decide_request(
                    cid, admin_id, req.request_id,
                    CdpiDecideRequest(
                        action=CdpiDecideAction.Approve,
                        expected_revision=req.revision - 1,
                        reason="Stale",
                    ),
                    direct_db,
                )
            assert exc.value.status_code == 409
        finally:
            await _cleanup_requests(direct_db, req.request_id)

    async def test_double_approve_is_idempotent_safe(self, direct_db):
        """Second approve on an already-Approved request must fail with 422."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="First approval",
            ),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc:
                await cdpi_service.decide_request(
                    cid, admin_id, req.request_id,
                    CdpiDecideRequest(
                        action=CdpiDecideAction.Approve,
                        expected_revision=result.revision,
                        reason="Duplicate approval",
                    ),
                    direct_db,
                )
            assert exc.value.status_code == 422
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)

    async def test_approve_only_one_branch_config_row_created(self, direct_db):
        """Approval for HQ request must not create a config row for PAYTEST."""
        cid, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        result = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Approve,
                expected_revision=req.revision,
                reason="OK",
            ),
            direct_db,
        )
        try:
            pid = result.approved_pay_item_id
            all_configs = (await direct_db.execute(
                _text("SELECT branchid FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": pid},
            )).scalars().all()
            assert len(all_configs) == 1
            assert all_configs[0] == hq_id
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                                pay_item_id=result.approved_pay_item_id)


# ===========================================================================
# Part B -- Direct Company Creation (12 tests)
# ===========================================================================

@pytest.mark.asyncio
class TestDirectCreateHappyPath:

    async def test_direct_create_returns_pay_item_id(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(
                item_name="Night Shift",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            assert isinstance(result.pay_item_id, int)
            assert result.company_id == cid
            assert result.item_name == "Night Shift"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_pay_item_code_has_cdpi_prefix(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(
                item_name="Overtime",
                input_type="Time",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            assert result.pay_item_code.startswith("CDPI")
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_pay_item_row_has_correct_fields(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(
                item_name="Loads",
                input_type="Number",
                calc_method_key="PerUnit",
                unit="trips",
                notes="Per trip bonus",
            ),
            direct_db,
        )
        try:
            row = (await direct_db.execute(
                _text("SELECT * FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": result.pay_item_id},
            )).mappings().first()
            assert row is not None
            assert row["payitemname"] == "Loads"
            assert row["companyid"] == cid
            assert row["branchid"] is None
            assert row["requestingbranchid"] is None
            assert row["category"] == "Custom"
            assert row["datatype"] == "Decimal"
            assert row["itemscope"] == "Daily"
            assert row["ratebehavior"] == "PerUnit"
            assert row["status"] == "Active"
            assert row["issystemstandard"] is False
            assert row["isdefaultbranchactive"] is False
            assert row["unit"] == "trips"
            assert row["notes"] == "Per trip bonus"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_cdpi_definition_row_exists(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(
                item_name="Hazard Pay",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            defn = (await direct_db.execute(
                _text("SELECT * FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
                {"pid": result.pay_item_id},
            )).mappings().first()
            assert defn is not None
            assert defn["definitionschemaversion"] == 1
            assert defn["lockedatutc"] is not None
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_no_branch_pay_item_config_rows(self, direct_db):
        """Direct create must not create BranchPayItemConfig rows for any branch."""
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(
                item_name="Hazard Pay",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            count = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": result.pay_item_id},
            )).scalar_one()
            assert count == 0
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_no_rate_rows(self, direct_db):
        """No PayItemRateTypeMap rows must be created."""
        cid, _, _, admin_id = await _get_ids(direct_db)
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(
                item_name="Night Allowance",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            rm = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                {"pid": result.pay_item_id},
            )).scalar_one()
            assert rm == 0
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=result.pay_item_id)

    async def test_direct_create_multiple_items_get_unique_codes(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        r1 = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="Item Alpha", input_type="Number",
                                     calc_method_key="PerUnit"),
            direct_db,
        )
        r2 = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="Item Beta", input_type="Time",
                                     calc_method_key="PerUnit"),
            direct_db,
        )
        try:
            assert r1.pay_item_code != r2.pay_item_code
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=r1.pay_item_id)
            await _cleanup_pay_item(direct_db, pay_item_id=r2.pay_item_id)


@pytest.mark.asyncio
class TestDirectCreateGuards:

    async def test_direct_create_requires_company_scope(self, direct_db):
        """SpecificBranch user with payitems.edit must get 403."""
        cid, hq_id, _, _ = await _get_ids(direct_db)
        uid = await _create_test_user(direct_db, company_id=cid,
                                       username=f"dc_branch_{uuid.uuid4().hex[:6]}")
        role_id = await _create_company_role(direct_db, company_id=cid,
                                              role_code=f"DC_BR_{uuid.uuid4().hex[:6]}",
                                              perms=["payitems.edit"])
        await _assign_role(direct_db, user_id=uid, company_id=cid,
                           role_id=role_id, scope="SpecificBranch", branch_id=hq_id)
        try:
            with pytest.raises(HTTPException) as exc:
                await cdpi_service.create_direct_company_item(
                    cid, uid,
                    CdpiDirectCreateRequest(
                        item_name="Should Fail",
                        input_type="Number",
                        calc_method_key="PerUnit",
                    ),
                    direct_db,
                )
            assert exc.value.status_code == 403
        finally:
            await _cleanup_user(direct_db, uid)
            await _cleanup_role(direct_db, role_id)

    async def test_direct_create_unimplemented_method_returns_422(self, direct_db):
        cid, _, _, admin_id = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc:
            await cdpi_service.create_direct_company_item(
                cid, admin_id,
                CdpiDirectCreateRequest(
                    item_name="Block Item",
                    input_type="Number",
                    calc_method_key="Block",
                ),
                direct_db,
            )
        assert exc.value.status_code == 422
        assert "PerUnit" in exc.value.detail


@pytest.mark.asyncio
class TestDirectCreateSchema:

    async def test_direct_create_requires_item_name(self):
        with pytest.raises(ValidationError):
            CdpiDirectCreateRequest(input_type="Number", calc_method_key="PerUnit")

    async def test_direct_create_requires_input_type(self):
        with pytest.raises(ValidationError):
            CdpiDirectCreateRequest(item_name="Test", calc_method_key="PerUnit")

    async def test_direct_create_requires_calc_method_key(self):
        with pytest.raises(ValidationError):
            CdpiDirectCreateRequest(item_name="Test", input_type="Number")

    async def test_direct_create_empty_item_name_rejected(self):
        with pytest.raises(ValidationError):
            CdpiDirectCreateRequest(item_name="  ", input_type="Number",
                                     calc_method_key="PerUnit")

    async def test_direct_create_invalid_input_type_rejected(self):
        with pytest.raises(ValidationError):
            CdpiDirectCreateRequest(item_name="Test", input_type="Hours",
                                     calc_method_key="PerUnit")

    async def test_direct_create_invalid_calc_method_key_rejected(self):
        with pytest.raises(ValidationError):
            CdpiDirectCreateRequest(item_name="Test", input_type="Number",
                                     calc_method_key="Unknown")

    async def test_direct_create_unit_and_notes_are_optional(self):
        body = CdpiDirectCreateRequest(item_name="Miles", input_type="Number",
                                        calc_method_key="PerUnit")
        assert body.unit is None
        assert body.notes is None


# ===========================================================================
# Part C -- Regression (4 tests)
# ===========================================================================

@pytest.mark.asyncio
class TestRegressionExistingDecideActions:
    """Existing decide actions must still work with the extended enum."""

    async def test_return_to_draft_still_works(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        try:
            result = await cdpi_service.decide_request(
                cid, admin_id, req.request_id,
                CdpiDecideRequest(
                    action=CdpiDecideAction.ReturnToDraft,
                    expected_revision=req.revision,
                    reason="Needs more info",
                ),
                direct_db,
            )
            assert result.status == "Draft"
        finally:
            await _cleanup_requests(direct_db, req.request_id)

    async def test_reject_still_works(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        try:
            result = await cdpi_service.decide_request(
                cid, admin_id, req.request_id,
                CdpiDecideRequest(
                    action=CdpiDecideAction.Reject,
                    expected_revision=req.revision,
                    reason="Not needed",
                ),
                direct_db,
            )
            assert result.status == "Rejected"
        finally:
            await _cleanup_requests(direct_db, req.request_id)

    async def test_submit_still_works(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await cdpi_service.create_draft(
            cid, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Fuel Allowance",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        try:
            result = await cdpi_service.submit_draft(
                cid, admin_id, draft.request_id,
                CdpiSubmitRequest(expected_revision=1),
                direct_db,
            )
            assert result.status == "PendingCompanyApproval"
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_copy_rejected_still_works(self, direct_db):
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id)
        rejected = await cdpi_service.decide_request(
            cid, admin_id, req.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Reject,
                expected_revision=req.revision,
                reason="Not needed",
            ),
            direct_db,
        )
        try:
            copy = await cdpi_service.copy_rejected(
                cid, admin_id, req.request_id, direct_db,
            )
            assert copy.status == "Draft"
            assert copy.copied_from_request_id == req.request_id
        finally:
            await _cleanup_requests(direct_db, copy.request_id, req.request_id)
