"""
test_cdpi_contracts.py

Tests for Task 2: common CDPI backend contracts and permission guards.

Part A — schema/contract tests (pure Python, no DB):
  CdpiStatus, CdpiEventType, CdpiInputType, CdpiCalcMethodKey,
  CdpiRequestDraftFields validators, pure helper functions.

Part B — permission guard tests (integration, uses direct_db):
  require_cdpi_branch_edit and require_cdpi_company_edit.
"""
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text as _text

from app.cdpi.guards import require_cdpi_branch_edit, require_cdpi_company_edit
from app.cdpi.schemas import (
    _MAX_UNIT_LENGTH,
    CdpiCalcMethodKey,
    CdpiEventType,
    CdpiInputType,
    CdpiRequestDraftFields,
    CdpiStatus,
    validate_calc_method_key,
    validate_input_type,
)

# ===========================================================================
# Part A — contracts (pure Python, no DB)
# ===========================================================================

class TestCdpiStatus:
    def test_valid_statuses(self):
        assert set(CdpiStatus) == {
            CdpiStatus.Draft,
            CdpiStatus.PendingCompanyApproval,
            CdpiStatus.Rejected,
            CdpiStatus.Approved,
        }

    def test_returned_to_draft_is_not_a_status(self):
        """ReturnedToDraft must be absent from CdpiStatus — it is a CdpiEventType."""
        status_values = {s.value for s in CdpiStatus}
        assert "ReturnedToDraft" not in status_values, (
            "ReturnedToDraft must be an event type, not a request status"
        )

    def test_draft_is_the_returned_status(self):
        """Returned requests use Draft again — verify Draft exists."""
        assert CdpiStatus.Draft.value == "Draft"


class TestCdpiEventType:
    def test_returned_to_draft_is_an_event(self):
        assert CdpiEventType.ReturnedToDraft.value == "ReturnedToDraft"

    def test_all_confirmed_event_types_present(self):
        expected = {
            "DraftCreated", "Submitted", "Resubmitted",
            "ReturnedToDraft", "Rejected", "Approved", "CopiedFromRejected",
        }
        assert {e.value for e in CdpiEventType} == expected


class TestCdpiInputType:
    def test_only_time_and_number(self):
        assert {e.value for e in CdpiInputType} == {"Time", "Number"}

    def test_validate_time(self):
        assert validate_input_type("Time") == "Time"

    def test_validate_number(self):
        assert validate_input_type("Number") == "Number"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="input_type"):
            validate_input_type("Hours")

    def test_same_rate_per_unit_is_invalid(self):
        """SameRatePerUnit must not be accepted."""
        with pytest.raises(ValueError):
            validate_input_type("SameRatePerUnit")


class TestCdpiCalcMethodKey:
    def test_all_five_keys_accepted(self):
        expected = {"PerUnit", "OrdinalTier", "Block", "RangeBracket", "RangeProgressive"}
        assert {e.value for e in CdpiCalcMethodKey} == expected

    def test_each_key_validates(self):
        for key in ("PerUnit", "OrdinalTier", "Block", "RangeBracket", "RangeProgressive"):
            assert validate_calc_method_key(key) == key

    def test_invalid_key_raises(self):
        with pytest.raises(ValueError, match="calc_method_key"):
            validate_calc_method_key("SameRatePerUnit")

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError):
            validate_calc_method_key("FlatFee")


class TestCdpiRequestDraftFields:
    """CdpiRequestDraftFields — write-side schema for Draft create/update."""

    def test_all_fields_optional(self):
        """Empty payload is valid — partial/autosave pattern."""
        obj = CdpiRequestDraftFields()
        assert obj.item_name is None
        assert obj.input_type is None
        assert obj.unit is None
        assert obj.calc_method_key is None
        assert obj.notes is None

    # -----------------------------------------------------------------------
    # input_type
    # -----------------------------------------------------------------------

    def test_input_type_time_accepted(self):
        obj = CdpiRequestDraftFields(input_type="Time")
        assert obj.input_type == "Time"

    def test_input_type_number_accepted(self):
        obj = CdpiRequestDraftFields(input_type="Number")
        assert obj.input_type == "Number"

    def test_input_type_invalid_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            CdpiRequestDraftFields(input_type="Hours")
        assert "input_type" in str(exc_info.value)

    def test_input_type_same_rate_per_unit_rejected(self):
        with pytest.raises(ValidationError):
            CdpiRequestDraftFields(input_type="SameRatePerUnit")

    # -----------------------------------------------------------------------
    # unit — always optional; works for both Time and Number
    # -----------------------------------------------------------------------

    def test_unit_optional_with_time(self):
        obj = CdpiRequestDraftFields(input_type="Time", unit=None)
        assert obj.unit is None

    def test_unit_optional_with_number(self):
        obj = CdpiRequestDraftFields(input_type="Number", unit=None)
        assert obj.unit is None

    def test_unit_accepted_with_time(self):
        obj = CdpiRequestDraftFields(input_type="Time", unit="Hrs")
        assert obj.unit == "Hrs"

    def test_unit_accepted_with_number(self):
        obj = CdpiRequestDraftFields(input_type="Number", unit="Miles")
        assert obj.unit == "Miles"

    def test_unit_too_long_rejected(self):
        long_unit = "X" * (_MAX_UNIT_LENGTH + 1)
        with pytest.raises(ValidationError) as exc_info:
            CdpiRequestDraftFields(unit=long_unit)
        assert "unit" in str(exc_info.value)

    def test_unit_max_length_accepted(self):
        at_limit = "X" * _MAX_UNIT_LENGTH
        obj = CdpiRequestDraftFields(unit=at_limit)
        assert len(obj.unit) == _MAX_UNIT_LENGTH

    # -----------------------------------------------------------------------
    # calc_method_key
    # -----------------------------------------------------------------------

    def test_all_calc_method_keys_accepted(self):
        for key in ("PerUnit", "OrdinalTier", "Block", "RangeBracket", "RangeProgressive"):
            obj = CdpiRequestDraftFields(calc_method_key=key)
            assert obj.calc_method_key == key

    def test_invalid_calc_method_key_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            CdpiRequestDraftFields(calc_method_key="SameRatePerUnit")
        assert "calc_method_key" in str(exc_info.value)

    # -----------------------------------------------------------------------
    # item_name
    # -----------------------------------------------------------------------

    def test_item_name_too_long_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            CdpiRequestDraftFields(item_name="X" * 201)
        assert "item_name" in str(exc_info.value)

    def test_item_name_200_chars_accepted(self):
        obj = CdpiRequestDraftFields(item_name="A" * 200)
        assert len(obj.item_name) == 200


# ===========================================================================
# Part B — permission guards (integration, uses direct_db)
# ===========================================================================

# ---------------------------------------------------------------------------
# Setup helpers — create minimal test users and roles via direct_db
# ---------------------------------------------------------------------------

async def _get_ids(db):
    """Return (company_id, hq_branch_id, paytest_branch_id, admin_user_id, viewer_user_id)."""
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
    viewer_id = (await db.execute(
        _text("SELECT userid FROM sec.users WHERE username = 'branch_user'")
    )).scalar_one()
    return company_id, hq_id, paytest_id, admin_id, viewer_id


async def _create_test_user(db, *, company_id: int, username: str) -> int:
    """Insert a minimal sec.Users row and return its UserID."""
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
                                perms: list[str]) -> int:
    """Insert a sec.CompanyRoles row with given permissions. Returns company_role_id."""
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
                        branch_id: int | None = None) -> None:
    await db.execute(
        _text("""
            INSERT INTO sec.userbranchroles
                (userid, companyid, branchid, companyroleId, scopetype, isactive)
            VALUES (:uid, :cid, :bid, :rid, :scope, TRUE)
        """),
        {
            "uid": user_id, "cid": company_id, "bid": branch_id,
            "rid": role_id, "scope": scope,
        },
    )


async def _cleanup_test_user(db, user_id: int) -> None:
    """Remove test user and all their role assignments."""
    await db.execute(
        _text("DELETE FROM sec.userbranchroles WHERE userid = :uid"), {"uid": user_id}
    )
    await db.execute(
        _text("DELETE FROM sec.users WHERE userid = :uid"), {"uid": user_id}
    )


async def _cleanup_company_role(db, role_id: int) -> None:
    await db.execute(
        _text("DELETE FROM sec.companyrolepermissions WHERE companyroleid = :rid"),
        {"rid": role_id},
    )
    await db.execute(
        _text("DELETE FROM sec.companyroles WHERE companyroleid = :rid"), {"rid": role_id}
    )


# ---------------------------------------------------------------------------
# Branch guard tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRequireCdpiBranchEdit:

    async def test_admin_passes_for_hq(self, direct_db):
        """Admin (AllCompanyBranches + all perms including payitems.edit) passes."""
        cid, hq, _, admin, _ = await _get_ids(direct_db)
        await require_cdpi_branch_edit(cid, admin, hq, direct_db)  # must not raise

    async def test_admin_passes_for_paytest(self, direct_db):
        """AllCompanyBranches scope gives access to any branch."""
        cid, _, paytest, admin, _ = await _get_ids(direct_db)
        await require_cdpi_branch_edit(cid, admin, paytest, direct_db)

    async def test_viewer_no_payitems_edit_fails(self, direct_db):
        """branch_user has payroll.view only — no payitems.edit — must be denied."""
        cid, hq, _, _, viewer = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc_info:
            await require_cdpi_branch_edit(cid, viewer, hq, direct_db)
        assert exc_info.value.status_code == 403

    async def test_specific_branch_user_passes_own_branch(self, direct_db):
        """SpecificBranch HQ user with payitems.edit passes for HQ."""
        cid, hq, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_BR_{suffix}",
            perms=["payitems.view", "payitems.edit"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_br_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="SpecificBranch", branch_id=hq,
        )
        try:
            await require_cdpi_branch_edit(cid, user_id, hq, direct_db)  # must pass
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_specific_branch_user_fails_other_branch(self, direct_db):
        """SpecificBranch HQ user with payitems.edit is denied for PAYTEST."""
        cid, hq, paytest, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_BR2_{suffix}",
            perms=["payitems.view", "payitems.edit"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_br2_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="SpecificBranch", branch_id=hq,  # only HQ
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_branch_edit(cid, user_id, paytest, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_payroll_entry_alone_fails(self, direct_db):
        """payroll.entry does not grant CDPI branch edit."""
        cid, hq, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_ENTRY_{suffix}",
            perms=["payroll.view", "payroll.entry"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_entry_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_branch_edit(cid, user_id, hq, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_setup_manage_alone_fails(self, direct_db):
        """setup.manage without payitems.edit does not grant CDPI branch edit."""
        cid, hq, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_SETUP_{suffix}",
            perms=["settings.view", "setup.manage"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_setup_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_branch_edit(cid, user_id, hq, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_settings_manage_alone_fails(self, direct_db):
        """settings.manage without payitems.edit does not grant CDPI branch edit."""
        cid, hq, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_SMGR_{suffix}",
            perms=["settings.view", "settings.manage"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_smgr_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_branch_edit(cid, user_id, hq, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)


# ---------------------------------------------------------------------------
# Company guard tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRequireCdpiCompanyEdit:

    async def test_admin_passes(self, direct_db):
        """Admin (AllCompanyBranches + payitems.edit) passes company guard."""
        cid, _, _, admin, _ = await _get_ids(direct_db)
        await require_cdpi_company_edit(cid, admin, direct_db)  # must not raise

    async def test_viewer_fails(self, direct_db):
        """branch_user (SpecificBranch + no payitems.edit) fails company guard."""
        cid, _, _, _, viewer = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc_info:
            await require_cdpi_company_edit(cid, viewer, direct_db)
        assert exc_info.value.status_code == 403

    async def test_all_branches_payitems_edit_passes(self, direct_db):
        """AllCompanyBranches user with payitems.edit passes company guard."""
        cid, _, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_CO_{suffix}",
            perms=["payitems.view", "payitems.edit"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_co_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            await require_cdpi_company_edit(cid, user_id, direct_db)  # must pass
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_specific_branch_payitems_edit_fails(self, direct_db):
        """SpecificBranch user with payitems.edit is denied company guard.

        Company-level actions require AllCompanyBranches scope.
        Branch-scoped payitems.edit must not pass as company-wide authority.
        """
        cid, hq, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_SBCO_{suffix}",
            perms=["payitems.view", "payitems.edit"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_sbco_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="SpecificBranch", branch_id=hq,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_company_edit(cid, user_id, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_payroll_entry_alone_fails(self, direct_db):
        """payroll.entry does not grant CDPI company edit."""
        cid, _, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_COENT_{suffix}",
            perms=["payroll.view", "payroll.entry"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_coent_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_company_edit(cid, user_id, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_setup_manage_alone_fails(self, direct_db):
        """setup.manage without payitems.edit does not grant CDPI company edit."""
        cid, _, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_COSETP_{suffix}",
            perms=["settings.view", "setup.manage"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_cosetp_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_company_edit(cid, user_id, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)

    async def test_settings_manage_alone_fails(self, direct_db):
        """settings.manage without payitems.edit does not grant CDPI company edit."""
        cid, _, _, _, _ = await _get_ids(direct_db)

        suffix = uuid.uuid4().hex[:6]
        role_id = await _create_company_role(
            direct_db, company_id=cid,
            role_code=f"CDPI_COSMGR_{suffix}",
            perms=["settings.view", "settings.manage"],
        )
        user_id = await _create_test_user(
            direct_db, company_id=cid, username=f"cdpi_cosmgr_{suffix}"
        )
        await _assign_role(
            direct_db, user_id=user_id, company_id=cid, role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await require_cdpi_company_edit(cid, user_id, direct_db)
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_test_user(direct_db, user_id)
            await _cleanup_company_role(direct_db, role_id)
