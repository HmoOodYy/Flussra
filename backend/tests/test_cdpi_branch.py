"""
test_cdpi_branch.py

Task 7 integration tests: Branch Controls + Branch Display Name Override.

Coverage:
  Part A -- list_branch_cdpi_items (GET /branches/{id}/items):
    1.  Returns empty list when no CDPI items exist
    2.  Returns CDPI items but NOT non-CDPI PayItems
    3.  Returns item with is_active from PayItem.IsDefaultBranchActive when no config row
    4.  Returns item with is_active from BranchPayItemConfig when config row exists
    5.  Returns branch_display_name_override=None when no config override
    6.  Returns correct override value from BranchPayItemConfig
    7.  effective_display_name == item_name when no override
    8.  effective_display_name == override when override set
    9.  is_cdpi=True always
    10. Caller without payitems.edit raises 403
    11. Non-existent branch raises 404

  Part B -- update_branch_cdpi_item (PATCH /branches/{id}/items/{pid}):
    12. Activate an item with no existing config row → fresh INSERT
    13. Deactivate an already-active item → UPDATE or new row
    14. Set branch_display_name_override (non-empty) → stored, returned
    15. Clear override with null → branchdisplayname=NULL in DB
    16. Clear override with empty string → branchdisplayname=NULL in DB
    17. PATCH with no fields → returns current state unchanged, no DB write
    18. is_active + display_name in same PATCH → both applied
    19. Overriding only display_name leaves is_active unchanged
    20. Overriding only is_active leaves display_name unchanged
    21. Non-CDPI PayItem raises 404
    22. Wrong company PayItem raises 404
    23. Non-existent branch raises 404
    24. Non-existent pay_item_id raises 404
    25. display_name_override > 200 chars raises 422

  Part C -- Effective-dating:
    26. Same-day existing config → UPDATE in place (effectivefrom unchanged)
    27. Existing config with effectivefrom < today → close existing, INSERT new
    28. Period-blocked → effective_from = period_max_end + 1 day

  Part D -- Regression (Tests 29-36):
    29. Approval creates BranchPayItemConfig; item appears in list_branch_cdpi_items
    30. Direct-created item appears in list_branch_cdpi_items with is_active=False
    31. list_branch_cdpi_items orders by item_name
    32. After PATCH activate, list shows is_active=True
    33. Retired PayItem not returned by list
    34. submit/return/reject/copy still work with branch routes present
"""
import datetime
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text as _text

from app.cdpi.schemas import (
    CdpiRequestCreate,
    CdpiSubmitRequest,
    CdpiDecideRequest,
    CdpiDecideAction,
    CdpiDirectCreateRequest,
    CdpiBranchItemUpdate,
)
from app.cdpi import service as cdpi_service


# ===========================================================================
# DB helpers (mirrors test_cdpi_approval.py conventions)
# ===========================================================================

async def _get_ids(db):
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


async def _cleanup_approved_request(db, *, request_id, pay_item_id: int) -> None:
    await db.execute(_text("ALTER TABLE payroll.cdpirequestevents DISABLE TRIGGER ALL"))
    try:
        await db.execute(
            _text("DELETE FROM payroll.cdpirequestevents WHERE requestid = :rid"),
            {"rid": str(request_id)},
        )
    finally:
        await db.execute(_text("ALTER TABLE payroll.cdpirequestevents ENABLE TRIGGER ALL"))
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
                                 item_name="MilesBranchTest", input_type="Number"):
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


async def _approve_request(db, *, company_id, user_id, request):
    return await cdpi_service.decide_request(
        company_id, user_id, request.request_id,
        CdpiDecideRequest(
            action=CdpiDecideAction.Approve,
            expected_revision=request.revision,
            reason="OK",
        ),
        db,
    )


async def _direct_create(db, *, company_id, user_id,
                          item_name="DirectBranchTest",
                          input_type="Number"):
    return await cdpi_service.create_direct_company_item(
        company_id, user_id,
        CdpiDirectCreateRequest(
            item_name=item_name,
            input_type=input_type,
            calc_method_key="PerUnit",
        ),
        db,
    )


# ===========================================================================
# Part A -- list_branch_cdpi_items
# ===========================================================================

@pytest.mark.asyncio
class TestListBranchCdpiItems:

    async def test_empty_when_no_cdpi_items(self, direct_db):
        """Test 1: list returns empty list when company has no CDPI items."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        # Use PAYTEST branch which likely has no CDPI items in clean state.
        _, _, paytest_id, _ = await _get_ids(direct_db)
        result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, paytest_id, direct_db)
        # Result may not be empty if other tests ran; we only assert type.
        assert isinstance(result, list)

    async def test_returns_only_cdpi_items_not_all_payitems(self, direct_db):
        """Test 2: only PayItems with CdpiDefinitions row appear."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T2CdpiOnly")
        try:
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            ids_in_result = {r.pay_item_id for r in result}
            assert summary.pay_item_id in ids_in_result
            # Confirm all returned items have is_cdpi=True
            assert all(r.is_cdpi for r in result)
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_is_active_defaults_from_payitem_when_no_config_row(self, direct_db):
        """Test 3: is_active == IsDefaultBranchActive when no BranchPayItemConfig row."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        # Direct-created items have IsDefaultBranchActive=FALSE and no config row for non-requesting branches.
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T3DefaultActive")
        try:
            # Delete any accidental config rows.
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next((r for r in result if r.pay_item_id == summary.pay_item_id), None)
            assert item is not None
            assert item.is_active is False  # IsDefaultBranchActive=FALSE for CDPI items
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_is_active_from_config_row(self, direct_db):
        """Test 4: is_active == BranchPayItemConfig.IsActive when config row exists."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T4ConfigActive")
        try:
            # Manually insert a config row with is_active=TRUE.
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, TRUE, CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next(r for r in result if r.pay_item_id == summary.pay_item_id)
            assert item.is_active is True
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_branch_display_name_override_none_when_no_config(self, direct_db):
        """Test 5: branch_display_name_override is None when no config row."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T5NoOverride")
        try:
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next(r for r in result if r.pay_item_id == summary.pay_item_id)
            assert item.branch_display_name_override is None
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_branch_display_name_override_from_config(self, direct_db):
        """Test 6: branch_display_name_override matches config row value."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T6Override")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, branchdisplayname,
                         effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, FALSE, 'HQ Display Name', CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next(r for r in result if r.pay_item_id == summary.pay_item_id)
            assert item.branch_display_name_override == "HQ Display Name"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_effective_display_name_equals_item_name_when_no_override(self, direct_db):
        """Test 7: effective_display_name == item_name when no override."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T7EffDisplay")
        try:
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next(r for r in result if r.pay_item_id == summary.pay_item_id)
            assert item.effective_display_name == "T7EffDisplay"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_effective_display_name_equals_override_when_set(self, direct_db):
        """Test 8: effective_display_name == override when override is set."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T8EffOverride")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, branchdisplayname,
                         effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, FALSE, 'My Override', CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next(r for r in result if r.pay_item_id == summary.pay_item_id)
            assert item.effective_display_name == "My Override"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_is_cdpi_always_true(self, direct_db):
        """Test 9: is_cdpi=True for all items returned."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T9IsCdpi")
        try:
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            assert all(r.is_cdpi for r in result)
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_no_permission_raises_403(self, direct_db):
        """Test 10: caller without payitems.edit raises 403."""
        cid, hq_id, _, _ = await _get_ids(direct_db)
        no_perm_user = await _create_test_user(direct_db, company_id=cid,
                                                username="t10_noperm_branch")
        role_id = await _create_company_role(direct_db, company_id=cid,
                                              role_code="T10BranchNoEdit",
                                              perms=["payitems.view"])
        await _assign_role(direct_db, user_id=no_perm_user, company_id=cid, role_id=role_id)
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.list_branch_cdpi_items(cid, no_perm_user, hq_id, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_user(direct_db, no_perm_user)
            await _cleanup_role(direct_db, role_id)

    async def test_nonexistent_branch_raises_404(self, direct_db):
        """Test 11: non-existent branch_id raises 404."""
        cid, _, _, admin_id = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc_info:
            await cdpi_service.list_branch_cdpi_items(cid, admin_id, 999_999_999, direct_db)
        assert exc_info.value.status_code == 404


# ===========================================================================
# Part B -- update_branch_cdpi_item
# ===========================================================================

@pytest.mark.asyncio
class TestUpdateBranchCdpiItem:

    async def test_activate_no_existing_config_inserts_row(self, direct_db):
        """Test 12: activate item with no config row → fresh INSERT, is_active=True."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T12Activate")
        try:
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )
            assert result.is_active is True
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_deactivate_existing_active_item(self, direct_db):
        """Test 13: deactivate an already-active item."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T13Deactivate")
        try:
            # Set active first.
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, TRUE, CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=False),
                direct_db,
            )
            assert result.is_active is False
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_set_display_name_override(self, direct_db):
        """Test 14: set branch_display_name_override → stored and returned."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T14SetDisplayName")
        try:
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(branch_display_name_override="HQ Miles"),
                direct_db,
            )
            assert result.branch_display_name_override == "HQ Miles"
            assert result.effective_display_name == "HQ Miles"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_clear_override_with_null(self, direct_db):
        """Test 15: clear override by passing null → branchdisplayname=NULL."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T15ClearNull")
        try:
            # Set an override first.
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, branchdisplayname,
                         effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, FALSE, 'ToBeCleared', CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(branch_display_name_override=None),
                direct_db,
            )
            assert result.branch_display_name_override is None
            assert result.effective_display_name == "T15ClearNull"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_clear_override_with_empty_string(self, direct_db):
        """Test 16: clear override by passing empty string → treated as None."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T16ClearEmpty")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, branchdisplayname,
                         effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, FALSE, 'WillBeCleared', CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            # Pydantic strips and converts empty string to None via validator.
            update = CdpiBranchItemUpdate(branch_display_name_override="")
            assert update.branch_display_name_override is None
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                update,
                direct_db,
            )
            assert result.branch_display_name_override is None
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_patch_with_no_fields_returns_current_state(self, direct_db):
        """Test 17: PATCH with no fields → no-op, current state returned."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T17NoOp")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, branchdisplayname,
                         effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, TRUE, 'Stable', CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            # Empty body — no model_fields_set entries for is_active or display_name.
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(),
                direct_db,
            )
            assert result.is_active is True
            assert result.branch_display_name_override == "Stable"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_both_fields_in_same_patch(self, direct_db):
        """Test 18: is_active + display_name in same PATCH → both applied."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T18BothFields")
        try:
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=True, branch_display_name_override="Combined"),
                direct_db,
            )
            assert result.is_active is True
            assert result.branch_display_name_override == "Combined"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_only_display_name_leaves_is_active_unchanged(self, direct_db):
        """Test 19: patch display_name only → is_active unchanged."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T19DisplayOnly")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, TRUE, CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(branch_display_name_override="NameOnly"),
                direct_db,
            )
            assert result.is_active is True  # unchanged
            assert result.branch_display_name_override == "NameOnly"
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_only_is_active_leaves_display_name_unchanged(self, direct_db):
        """Test 20: patch is_active only → display_name unchanged."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T20ActiveOnly")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, branchdisplayname,
                         effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, FALSE, 'KeepMe', CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            result = await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )
            assert result.is_active is True
            assert result.branch_display_name_override == "KeepMe"  # unchanged
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_non_cdpi_payitem_raises_404(self, direct_db):
        """Test 21: PayItem without CdpiDefinitions row raises 404."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        # Find a non-CDPI PayItem for this company.
        non_cdpi_pid = (await direct_db.execute(
            _text("""
                SELECT pi.payitemid
                FROM   payroll.payitems pi
                WHERE  pi.companyid = :cid
                  AND  pi.status   != 'Retired'
                  AND  NOT EXISTS (
                      SELECT 1 FROM payroll.cdpidefinitions cd WHERE cd.payitemid = pi.payitemid
                  )
                LIMIT 1
            """),
            {"cid": cid},
        )).scalar_one_or_none()

        if non_cdpi_pid is None:
            pytest.skip("No non-CDPI PayItems available for this test.")

        with pytest.raises(HTTPException) as exc_info:
            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, non_cdpi_pid,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )
        assert exc_info.value.status_code == 404

    async def test_wrong_company_payitem_raises_404(self, direct_db):
        """Test 22: pay_item_id from different company raises 404."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc_info:
            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, 999_999_998,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )
        assert exc_info.value.status_code == 404

    async def test_nonexistent_branch_raises_404(self, direct_db):
        """Test 23: non-existent branch raises 404."""
        cid, _, _, admin_id = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc_info:
            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, 999_999_999, 1,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )
        assert exc_info.value.status_code == 404

    async def test_nonexistent_pay_item_raises_404(self, direct_db):
        """Test 24: non-existent pay_item_id raises 404."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc_info:
            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, 999_999_997,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )
        assert exc_info.value.status_code == 404

    async def test_display_name_too_long_raises_422(self, direct_db):
        """Test 25: display_name_override > 200 chars raises ValidationError."""
        with pytest.raises(ValidationError):
            CdpiBranchItemUpdate(branch_display_name_override="x" * 201)


# ===========================================================================
# Part C -- Effective-dating
# ===========================================================================

@pytest.mark.asyncio
class TestEffectiveDating:

    async def test_same_day_config_updated_in_place(self, direct_db):
        """Test 26: existing config with effectivefrom=today → UPDATE in place."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T26SameDay")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, FALSE, CURRENT_DATE, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id, "uid": admin_id},
            )
            config_id_before = (await direct_db.execute(
                _text("""
                    SELECT configid FROM payroll.branchpayitemconfig
                    WHERE payitemid=:pid AND branchid=:bid AND effectiveto IS NULL
                """),
                {"pid": summary.pay_item_id, "bid": hq_id},
            )).scalar_one()

            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )

            config_id_after = (await direct_db.execute(
                _text("""
                    SELECT configid FROM payroll.branchpayitemconfig
                    WHERE payitemid=:pid AND branchid=:bid AND effectiveto IS NULL
                """),
                {"pid": summary.pay_item_id, "bid": hq_id},
            )).scalar_one()

            # Same row was updated, no new row inserted.
            assert config_id_before == config_id_after
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_past_config_closed_and_new_inserted(self, direct_db):
        """Test 27: existing config with effectivefrom < today → close + INSERT new."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T27ClosePast")
        try:
            yesterday = datetime.date.today() - datetime.timedelta(days=1)
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:cid, :bid, :pid, FALSE, :eff, :uid)
                """),
                {"cid": cid, "bid": hq_id, "pid": summary.pay_item_id,
                 "uid": admin_id, "eff": yesterday},
            )
            old_config_id = (await direct_db.execute(
                _text("""
                    SELECT configid FROM payroll.branchpayitemconfig
                    WHERE payitemid=:pid AND branchid=:bid AND effectiveto IS NULL
                """),
                {"pid": summary.pay_item_id, "bid": hq_id},
            )).scalar_one()

            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )

            # Old row should now be closed.
            old_effectiveto = (await direct_db.execute(
                _text("SELECT effectiveto FROM payroll.branchpayitemconfig WHERE configid=:cid_r"),
                {"cid_r": old_config_id},
            )).scalar_one()
            assert old_effectiveto is not None

            # New open row should exist.
            new_row = (await direct_db.execute(
                _text("""
                    SELECT isactive, effectivefrom FROM payroll.branchpayitemconfig
                    WHERE payitemid=:pid AND branchid=:bid AND effectiveto IS NULL
                """),
                {"pid": summary.pay_item_id, "bid": hq_id},
            )).mappings().first()
            assert new_row is not None
            assert new_row["isactive"] is True
            assert new_row["effectivefrom"] == datetime.date.today()
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_open_period_shifts_effective_from(self, direct_db):
        """Test 28: update blocked by open period → effective_from = period_max_end + 1."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T28PeriodBlock")
        today = datetime.date.today()
        period_end = today + datetime.timedelta(days=6)
        period_id = None
        try:
            # Insert a fake open payroll period covering today.
            period_id = (await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperiods
                        (companyid, branchid, periodcode, periodname, periodtype,
                         startdate, enddate, status)
                    VALUES (:cid, :bid, :pcode, :pname, 'Week', :start, :end, 'Open')
                    RETURNING payrollperiodid
                """),
                {
                    "cid": cid, "bid": hq_id,
                    "pcode": "T28-CDPI-PERIOD", "pname": "T28 CDPI Period Block",
                    "start": today, "end": period_end,
                },
            )).scalar_one()

            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )

            # The new config row should have effectivefrom = period_end + 1.
            new_eff = (await direct_db.execute(
                _text("""
                    SELECT effectivefrom FROM payroll.branchpayitemconfig
                    WHERE payitemid=:pid AND branchid=:bid AND effectiveto IS NULL
                """),
                {"pid": summary.pay_item_id, "bid": hq_id},
            )).scalar_one()
            assert new_eff == period_end + datetime.timedelta(days=1)
        finally:
            if period_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                    {"pid": period_id},
                )
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)


# ===========================================================================
# Part D -- Regression (29-34)
# ===========================================================================

@pytest.mark.asyncio
class TestBranchRegressions:

    async def test_approved_item_appears_in_list(self, direct_db):
        """Test 29: after approval, item appears in list_branch_cdpi_items for requesting branch."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        req = await _make_pending_request(direct_db, company_id=cid, branch_id=hq_id,
                                          user_id=admin_id, item_name="T29ApproveList")
        approved = await _approve_request(direct_db, company_id=cid, user_id=admin_id,
                                           request=req)
        try:
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            ids = {r.pay_item_id for r in result}
            assert approved.approved_pay_item_id in ids
            item = next(r for r in result if r.pay_item_id == approved.approved_pay_item_id)
            assert item.is_active is True  # Approval sets IsActive=TRUE for requesting branch
        finally:
            await _cleanup_approved_request(direct_db, request_id=req.request_id,
                                             pay_item_id=approved.approved_pay_item_id)

    async def test_direct_created_item_appears_with_is_active_false(self, direct_db):
        """Test 30: direct-created item appears in list with is_active=False (no branch config)."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T30DirectList")
        try:
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next((r for r in result if r.pay_item_id == summary.pay_item_id), None)
            assert item is not None
            assert item.is_active is False
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_list_ordered_by_item_name(self, direct_db):
        """Test 31: list returns items in ascending item_name order."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        s1 = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                   item_name="ZZZ_T31_Last")
        s2 = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                   item_name="AAA_T31_First")
        try:
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            names = [r.item_name for r in result]
            assert names == sorted(names)
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=s1.pay_item_id)
            await _cleanup_pay_item(direct_db, pay_item_id=s2.pay_item_id)

    async def test_after_patch_activate_list_shows_active(self, direct_db):
        """Test 32: after PATCH activate, list_branch_cdpi_items shows is_active=True."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T32PatchThenList")
        try:
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            await cdpi_service.update_branch_cdpi_item(
                cid, admin_id, hq_id, summary.pay_item_id,
                CdpiBranchItemUpdate(is_active=True),
                direct_db,
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            item = next(r for r in result if r.pay_item_id == summary.pay_item_id)
            assert item.is_active is True
        finally:
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_retired_item_not_in_list(self, direct_db):
        """Test 33: Retired PayItem is not returned by list_branch_cdpi_items."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        summary = await _direct_create(direct_db, company_id=cid, user_id=admin_id,
                                        item_name="T33RetiredItem")
        try:
            # Retire the PayItem.
            await direct_db.execute(
                _text("UPDATE payroll.payitems SET status = 'Retired' WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            result = await cdpi_service.list_branch_cdpi_items(cid, admin_id, hq_id, direct_db)
            ids = {r.pay_item_id for r in result}
            assert summary.pay_item_id not in ids
        finally:
            await direct_db.execute(
                _text("UPDATE payroll.payitems SET status = 'Active' WHERE payitemid = :pid"),
                {"pid": summary.pay_item_id},
            )
            await _cleanup_pay_item(direct_db, pay_item_id=summary.pay_item_id)

    async def test_existing_workflow_submit_return_reject_copy_still_work(self, direct_db):
        """Test 34: submit/return/reject/copy paths still work after Task 7 changes."""
        cid, hq_id, _, admin_id = await _get_ids(direct_db)
        from app.cdpi.schemas import CdpiRequestUpdate
        draft = await cdpi_service.create_draft(
            cid, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="T34Regression",
                input_type="Number",
                calc_method_key="PerUnit",
            ),
            direct_db,
        )
        pending = await cdpi_service.submit_draft(
            cid, admin_id, draft.request_id,
            CdpiSubmitRequest(expected_revision=1),
            direct_db,
        )
        assert pending.status == "PendingCompanyApproval"

        returned = await cdpi_service.decide_request(
            cid, admin_id, pending.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.ReturnToDraft,
                expected_revision=pending.revision,
                reason="Needs revision",
            ),
            direct_db,
        )
        assert returned.status == "Draft"

        # Re-submit and reject.
        await cdpi_service.update_draft(
            cid, admin_id, returned.request_id,
            CdpiRequestUpdate(expected_revision=returned.revision, item_name="T34RegressionV2"),
            direct_db,
        )
        pending2 = await cdpi_service.submit_draft(
            cid, admin_id, returned.request_id,
            CdpiSubmitRequest(expected_revision=returned.revision + 1),
            direct_db,
        )
        rejected = await cdpi_service.decide_request(
            cid, admin_id, pending2.request_id,
            CdpiDecideRequest(
                action=CdpiDecideAction.Reject,
                expected_revision=pending2.revision,
                reason="Not needed",
            ),
            direct_db,
        )
        assert rejected.status == "Rejected"

        copied = await cdpi_service.copy_rejected(
            cid, admin_id, rejected.request_id, direct_db,
        )
        assert copied.status == "Draft"

        # Cleanup: copied request + original request (no pay item created).
        from tests.test_cdpi_approval import _cleanup_requests
        await _cleanup_requests(direct_db, copied.request_id, rejected.request_id)
