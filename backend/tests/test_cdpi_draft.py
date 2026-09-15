"""
test_cdpi_draft.py

Task 3 integration tests: Draft CRUD + optimistic concurrency for CDPI requests.

All tests hit the real DB via direct_db (AUTOCOMMIT AsyncConnection).
Tests are isolated: each creates the rows it needs and cleans them up in a
finally block, even on failure.

Part A -- pure schema/validation (sync, no DB):
  TestCdpiRequestCreate, TestCdpiRequestUpdate

Part B -- service integration (async, direct_db):
  TestCreateDraft, TestGetRequest, TestListRequests, TestUpdateDraft
"""
import uuid
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text as _text

from app.cdpi.schemas import CdpiRequestCreate, CdpiRequestUpdate
from app.cdpi import service as cdpi_service


# ===========================================================================
# Shared DB helpers
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
    """Delete CDPI requests and their events safely."""
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


# ===========================================================================
# Part A -- pure schema/validation (sync, no DB)
# ===========================================================================

class TestCdpiRequestCreate:
    def test_requires_branch_id(self):
        with pytest.raises(ValidationError):
            CdpiRequestCreate()  # missing requesting_branch_id

    def test_minimal_create(self):
        body = CdpiRequestCreate(requesting_branch_id=1)
        assert body.requesting_branch_id == 1
        assert body.item_name is None
        assert body.input_type is None

    def test_all_fields_accepted(self):
        body = CdpiRequestCreate(
            requesting_branch_id=2,
            item_name="Hazard Pay",
            input_type="Number",
            unit="days",
            calc_method_key="PerUnit",
            notes="For hazardous shifts",
        )
        assert body.item_name == "Hazard Pay"
        assert body.input_type == "Number"

    def test_invalid_input_type_rejected(self):
        with pytest.raises(ValidationError):
            CdpiRequestCreate(requesting_branch_id=1, input_type="Hours")


class TestCdpiRequestUpdate:
    def test_requires_expected_revision(self):
        with pytest.raises(ValidationError):
            CdpiRequestUpdate()  # missing expected_revision

    def test_minimal_update(self):
        body = CdpiRequestUpdate(expected_revision=1)
        assert body.expected_revision == 1
        assert body.item_name is None

    def test_with_content_fields(self):
        body = CdpiRequestUpdate(
            expected_revision=3,
            item_name="Night Shift",
            calc_method_key="Block",
        )
        assert body.expected_revision == 3
        assert body.calc_method_key == "Block"

    def test_invalid_calc_method_rejected(self):
        with pytest.raises(ValidationError):
            CdpiRequestUpdate(expected_revision=1, calc_method_key="Unknown")


# ===========================================================================
# Part B -- service integration (async, direct_db)
# ===========================================================================

@pytest.mark.asyncio
class TestCreateDraft:
    async def test_create_minimal_draft(self, direct_db):
        """Successful create returns a Draft with Revision=1 and correct fields."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        result = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            assert result.status == "Draft"
            assert result.revision == 1
            assert result.company_id == company_id
            assert result.requesting_branch_id == hq_id
            assert result.created_by_user_id == admin_id
            assert result.approved_pay_item_id is None
        finally:
            await _cleanup_requests(direct_db, result.request_id)

    async def test_create_with_content_fields(self, direct_db):
        """Content fields supplied on creation are persisted correctly."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        body = CdpiRequestCreate(
            requesting_branch_id=hq_id,
            item_name="Night Allowance",
            input_type="Number",
            calc_method_key="PerUnit",
        )
        result = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            assert result.item_name == "Night Allowance"
            assert result.input_type == "Number"
            assert result.calc_method_key == "PerUnit"
        finally:
            await _cleanup_requests(direct_db, result.request_id)

    async def test_create_inserts_draft_created_event(self, direct_db):
        """A DraftCreated event row must exist after create."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        result = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.cdpirequestevents
                    WHERE requestid = :rid AND eventtype = 'DraftCreated'
                """),
                {"rid": str(result.request_id)},
            )).scalar_one()
            assert count == 1
        finally:
            await _cleanup_requests(direct_db, result.request_id)

    async def test_no_cdpi_definitions_created(self, direct_db):
        """Create draft must not insert any CdpiDefinitions rows."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        before = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.cdpidefinitions")
        )).scalar_one()
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        result = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            after = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.cdpidefinitions")
            )).scalar_one()
            assert after == before
        finally:
            await _cleanup_requests(direct_db, result.request_id)

    async def test_no_pay_items_created(self, direct_db):
        """Create draft must not insert any PayItems rows."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        before = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payitems")
        )).scalar_one()
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        result = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            after = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM payroll.payitems")
            )).scalar_one()
            assert after == before
        finally:
            await _cleanup_requests(direct_db, result.request_id)

    async def test_create_forbidden_without_permission(self, direct_db):
        """User without payitems.edit on the branch gets HTTP 403."""
        company_id, hq_id, _, _ = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        no_perm_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"noperm_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db,
            company_id=company_id,
            role_code=f"NOPERM_{suffix}",
            perms=[],
        )
        await _assign_role(
            direct_db,
            user_id=no_perm_user_id,
            company_id=company_id,
            role_id=role_id,
            scope="AllCompanyBranches",
        )
        try:
            body = CdpiRequestCreate(requesting_branch_id=hq_id)
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.create_draft(
                    company_id, no_perm_user_id, body, direct_db
                )
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_user(direct_db, no_perm_user_id)
            await _cleanup_role(direct_db, role_id)


@pytest.mark.asyncio
class TestGetRequest:
    async def test_get_returns_correct_row(self, direct_db):
        """get_request returns the row that was just created."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        body = CdpiRequestCreate(requesting_branch_id=hq_id, item_name="Get Test")
        created = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            fetched = await cdpi_service.get_request(
                company_id, admin_id, created.request_id, direct_db
            )
            assert fetched.request_id == created.request_id
            assert fetched.item_name == "Get Test"
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_get_missing_raises_404(self, direct_db):
        company_id, _, _, admin_id = await _get_ids(direct_db)
        fake_id = uuid.uuid4()
        with pytest.raises(HTTPException) as exc_info:
            await cdpi_service.get_request(company_id, admin_id, fake_id, direct_db)
        assert exc_info.value.status_code == 404

    async def test_get_scoped_branch_user_forbidden_on_other_branch(self, direct_db):
        """A SpecificBranch user on HQ cannot see a request belonging to PAYTEST."""
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]

        branch_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"buser_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db,
            company_id=company_id,
            role_code=f"BSCOPE_{suffix}",
            perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db,
            user_id=branch_user_id,
            company_id=company_id,
            role_id=role_id,
            scope="SpecificBranch",
            branch_id=hq_id,
        )

        body = CdpiRequestCreate(requesting_branch_id=paytest_id)
        created = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.get_request(
                    company_id, branch_user_id, created.request_id, direct_db
                )
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, created.request_id)
            await _cleanup_user(direct_db, branch_user_id)
            await _cleanup_role(direct_db, role_id)


@pytest.mark.asyncio
class TestListRequests:
    async def test_list_returns_created_request(self, direct_db):
        """list_requests includes a freshly created Draft."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        created = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            results = await cdpi_service.list_requests(company_id, admin_id, direct_db)
            ids = [r.request_id for r in results]
            assert created.request_id in ids
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_list_status_filter(self, direct_db):
        """Status filter restricts results to only the requested status."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        created = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            drafts = await cdpi_service.list_requests(
                company_id, admin_id, direct_db, status_filter="Draft"
            )
            assert all(r.status == "Draft" for r in drafts)
            assert created.request_id in [r.request_id for r in drafts]

            approved = await cdpi_service.list_requests(
                company_id, admin_id, direct_db, status_filter="Approved"
            )
            assert created.request_id not in [r.request_id for r in approved]
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_list_branch_filter(self, direct_db):
        """Branch filter restricts results to a single requesting branch."""
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        body_hq = CdpiRequestCreate(requesting_branch_id=hq_id)
        body_pt = CdpiRequestCreate(requesting_branch_id=paytest_id)
        req_hq = await cdpi_service.create_draft(company_id, admin_id, body_hq, direct_db)
        req_pt = await cdpi_service.create_draft(company_id, admin_id, body_pt, direct_db)
        try:
            hq_only = await cdpi_service.list_requests(
                company_id, admin_id, direct_db, branch_id=hq_id
            )
            hq_ids = [r.request_id for r in hq_only]
            assert req_hq.request_id in hq_ids
            assert req_pt.request_id not in hq_ids
        finally:
            await _cleanup_requests(direct_db, req_hq.request_id, req_pt.request_id)

    async def test_list_scoped_user_sees_only_own_branch(self, direct_db):
        """SpecificBranch user does not see requests from branches they can't access."""
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]

        scoped_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"scoped_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db,
            company_id=company_id,
            role_code=f"SCOPED_{suffix}",
            perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db,
            user_id=scoped_user_id,
            company_id=company_id,
            role_id=role_id,
            scope="SpecificBranch",
            branch_id=hq_id,
        )

        body_hq = CdpiRequestCreate(requesting_branch_id=hq_id)
        body_pt = CdpiRequestCreate(requesting_branch_id=paytest_id)
        req_hq = await cdpi_service.create_draft(company_id, admin_id, body_hq, direct_db)
        req_pt = await cdpi_service.create_draft(company_id, admin_id, body_pt, direct_db)
        try:
            results = await cdpi_service.list_requests(
                company_id, scoped_user_id, direct_db
            )
            result_ids = [r.request_id for r in results]
            assert req_hq.request_id in result_ids
            assert req_pt.request_id not in result_ids
        finally:
            await _cleanup_requests(direct_db, req_hq.request_id, req_pt.request_id)
            await _cleanup_user(direct_db, scoped_user_id)
            await _cleanup_role(direct_db, role_id)


@pytest.mark.asyncio
class TestUpdateDraft:
    async def test_update_changes_fields_and_increments_revision(self, direct_db):
        """Successful update writes new field values and Revision becomes 2."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id, item_name="Old Name"),
            direct_db,
        )
        try:
            updated = await cdpi_service.update_draft(
                company_id, admin_id, created.request_id,
                CdpiRequestUpdate(expected_revision=1, item_name="New Name"),
                direct_db,
            )
            assert updated.item_name == "New Name"
            assert updated.revision == 2
            assert updated.updated_by_user_id == admin_id
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_update_stale_revision_raises_409(self, direct_db):
        """Supplying the wrong expected_revision raises HTTP 409."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.update_draft(
                    company_id, admin_id, created.request_id,
                    CdpiRequestUpdate(expected_revision=99),
                    direct_db,
                )
            assert exc_info.value.status_code == 409
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_update_no_event_written(self, direct_db):
        """A successful Draft update must not write a new event row."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id),
            direct_db,
        )
        try:
            await cdpi_service.update_draft(
                company_id, admin_id, created.request_id,
                CdpiRequestUpdate(expected_revision=1, notes="Changed"),
                direct_db,
            )
            count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.cdpirequestevents
                    WHERE requestid = :rid
                """),
                {"rid": str(created.request_id)},
            )).scalar_one()
            assert count == 1  # only the original DraftCreated event
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_update_missing_request_raises_404(self, direct_db):
        company_id, _, _, admin_id = await _get_ids(direct_db)
        with pytest.raises(HTTPException) as exc_info:
            await cdpi_service.update_draft(
                company_id, admin_id, uuid.uuid4(),
                CdpiRequestUpdate(expected_revision=1),
                direct_db,
            )
        assert exc_info.value.status_code == 404

    async def test_update_partial_fields_only(self, direct_db):
        """Omitted fields are preserved; only provided fields change."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Preserved",
                input_type="Number",
            ),
            direct_db,
        )
        try:
            updated = await cdpi_service.update_draft(
                company_id, admin_id, created.request_id,
                CdpiRequestUpdate(expected_revision=1, notes="New note"),
                direct_db,
            )
            assert updated.item_name == "Preserved"
            assert updated.input_type == "Number"
            assert updated.notes == "New note"
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_update_forbidden_without_permission(self, direct_db):
        """User without payitems.edit on the branch gets HTTP 403 on update."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        no_perm_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"npu_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db,
            company_id=company_id,
            role_code=f"NPU_{suffix}",
            perms=[],
        )
        await _assign_role(
            direct_db,
            user_id=no_perm_id,
            company_id=company_id,
            role_id=role_id,
            scope="AllCompanyBranches",
        )
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.update_draft(
                    company_id, no_perm_id, created.request_id,
                    CdpiRequestUpdate(expected_revision=1),
                    direct_db,
                )
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, created.request_id)
            await _cleanup_user(direct_db, no_perm_id)
            await _cleanup_role(direct_db, role_id)

    async def test_update_cross_branch_forbidden_403(self, direct_db):
        """SpecificBranch HQ user cannot PATCH a Draft belonging to PAYTEST."""
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        hq_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"xbr_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db, company_id=company_id,
            role_code=f"XBR_{suffix}", perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db, user_id=hq_user_id, company_id=company_id,
            role_id=role_id, scope="SpecificBranch", branch_id=hq_id,
        )
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=paytest_id, item_name="PayTest Item"),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.update_draft(
                    company_id, hq_user_id, created.request_id,
                    CdpiRequestUpdate(expected_revision=1, item_name="Hijacked"),
                    direct_db,
                )
            assert exc_info.value.status_code == 403

            row = (await direct_db.execute(
                _text("SELECT itemname, revision FROM payroll.cdpirequests WHERE requestid = :rid"),
                {"rid": str(created.request_id)},
            )).mappings().first()
            assert row["itemname"] == "PayTest Item"
            assert row["revision"] == 1
        finally:
            await _cleanup_requests(direct_db, created.request_id)
            await _cleanup_user(direct_db, hq_user_id)
            await _cleanup_role(direct_db, role_id)

    # -----------------------------------------------------------------------
    # Race-safe concurrency regression
    # -----------------------------------------------------------------------

    async def test_same_revision_second_update_rejected(self, direct_db):
        """
        Regression for the atomic conditional UPDATE pattern.

        Two sequential updates both supply expected_revision=1.
        The first must succeed (Revision -> 2).
        The second must be rejected with HTTP 409.
        The final row must reflect exactly one successful update.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id, item_name="Original"),
            direct_db,
        )
        try:
            # First update with revision=1 -- must succeed.
            first = await cdpi_service.update_draft(
                company_id, admin_id, created.request_id,
                CdpiRequestUpdate(expected_revision=1, item_name="Winner"),
                direct_db,
            )
            assert first.revision == 2
            assert first.item_name == "Winner"

            # Second update with the same revision=1 -- must be rejected.
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.update_draft(
                    company_id, admin_id, created.request_id,
                    CdpiRequestUpdate(expected_revision=1, item_name="Loser"),
                    direct_db,
                )
            assert exc_info.value.status_code == 409

            # Row must reflect only the first update.
            final = await cdpi_service.get_request(
                company_id, admin_id, created.request_id, direct_db
            )
            assert final.revision == 2
            assert final.item_name == "Winner"
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    # -----------------------------------------------------------------------
    # Non-Draft status rejection
    # -----------------------------------------------------------------------

    async def test_update_pending_approval_rejected_422(self, direct_db):
        """
        update_draft on a PendingCompanyApproval request raises HTTP 422.
        Row must not be modified.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id, item_name="PreSubmit"),
            direct_db,
        )
        try:
            # Force status to PendingCompanyApproval via direct DB write.
            await direct_db.execute(
                _text("""
                    UPDATE payroll.cdpirequests
                    SET status = 'PendingCompanyApproval'
                    WHERE requestid = :rid
                """),
                {"rid": str(created.request_id)},
            )

            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.update_draft(
                    company_id, admin_id, created.request_id,
                    CdpiRequestUpdate(expected_revision=1, item_name="Changed"),
                    direct_db,
                )
            assert exc_info.value.status_code == 422
            assert "PendingCompanyApproval" in exc_info.value.detail

            # Row must be unmodified (status still PendingCompanyApproval, revision still 1).
            row = (await direct_db.execute(
                _text("SELECT status, revision, itemname FROM payroll.cdpirequests WHERE requestid = :rid"),
                {"rid": str(created.request_id)},
            )).mappings().first()
            assert row["status"] == "PendingCompanyApproval"
            assert row["revision"] == 1
            assert row["itemname"] == "PreSubmit"
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_update_rejected_status_raises_422(self, direct_db):
        """
        update_draft on a Rejected request raises HTTP 422.
        Row must not be modified.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id, item_name="WasRejected"),
            direct_db,
        )
        try:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.cdpirequests
                    SET status = 'Rejected'
                    WHERE requestid = :rid
                """),
                {"rid": str(created.request_id)},
            )

            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.update_draft(
                    company_id, admin_id, created.request_id,
                    CdpiRequestUpdate(expected_revision=1),
                    direct_db,
                )
            assert exc_info.value.status_code == 422
            assert "Rejected" in exc_info.value.detail

            row = (await direct_db.execute(
                _text("SELECT status, revision FROM payroll.cdpirequests WHERE requestid = :rid"),
                {"rid": str(created.request_id)},
            )).mappings().first()
            assert row["status"] == "Rejected"
            assert row["revision"] == 1
        finally:
            await _cleanup_requests(direct_db, created.request_id)

    async def test_update_approved_status_raises_422(self, direct_db):
        """
        update_draft on an Approved request raises HTTP 422.

        Forcing Approved status in the test DB requires both:
          - ApprovedPayItemID IS NOT NULL  (lifecycle CHECK constraint)
          - (ApprovedPayItemID, CompanyID) must reference payroll.PayItems  (composite FK)

        Seed PayItems have companyid=NULL so the composite FK always rejects
        company-scoped requests.  We use SET session_replication_role = replica
        to suppress FK trigger enforcement for this one setup UPDATE while keeping
        the CHECK constraint active, then restore the default role.

        Row must not be modified by update_draft.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        created = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(requesting_branch_id=hq_id, item_name="WasApproved"),
            direct_db,
        )
        try:
            # Suppress FK triggers for the forced-status setup only.
            await direct_db.execute(_text("SET session_replication_role = replica"))
            try:
                await direct_db.execute(
                    _text("""
                        UPDATE payroll.cdpirequests
                        SET status = 'Approved', approvedpayitemid = 1
                        WHERE requestid = :rid
                    """),
                    {"rid": str(created.request_id)},
                )
            finally:
                await direct_db.execute(_text("SET session_replication_role = DEFAULT"))

            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.update_draft(
                    company_id, admin_id, created.request_id,
                    CdpiRequestUpdate(expected_revision=1),
                    direct_db,
                )
            assert exc_info.value.status_code == 422
            assert "Approved" in exc_info.value.detail

            row = (await direct_db.execute(
                _text("SELECT status, revision FROM payroll.cdpirequests WHERE requestid = :rid"),
                {"rid": str(created.request_id)},
            )).mappings().first()
            assert row["status"] == "Approved"
            assert row["revision"] == 1
        finally:
            # Suppress FK triggers again for cleanup (approvedpayitemid is set).
            await direct_db.execute(_text("SET session_replication_role = replica"))
            await _cleanup_requests(direct_db, created.request_id)
            await direct_db.execute(_text("SET session_replication_role = DEFAULT"))


# ===========================================================================
# No-legacy-write coverage
# ===========================================================================

@pytest.mark.asyncio
class TestNoLegacyWrites:
    """
    Create and update Draft requests must remain isolated from approved
    Pay Item infrastructure.  Each test verifies that a CDPI Draft CRUD
    operation inserts zero rows into the listed legacy tables.
    """

    async def _counts(self, db) -> dict:
        tables = [
            ("payroll", "payitems"),
            ("payroll", "cdpidefinitions"),
            ("payroll", "branchpayitemconfig"),
            ("payroll", "payitemsettings"),
            ("payroll", "ratetypes"),
            ("payroll", "payitemratetypemap"),
        ]
        counts = {}
        for schema, table in tables:
            counts[table] = (await db.execute(
                _text(f"SELECT COUNT(*) FROM {schema}.{table}")
            )).scalar_one()
        return counts

    async def test_create_draft_touches_no_legacy_tables(self, direct_db):
        """
        POST (create_draft) must not insert rows into PayItems,
        CdpiDefinitions, BranchPayItemConfig, PayItemSettings,
        RateTypes, or PayItemRateTypeMap.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        before = await self._counts(direct_db)
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        result = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        try:
            after = await self._counts(direct_db)
            for table, count in before.items():
                assert after[table] == count, (
                    f"create_draft unexpectedly inserted rows into {table}"
                )
        finally:
            await _cleanup_requests(direct_db, result.request_id)

    async def test_update_draft_touches_no_legacy_tables(self, direct_db):
        """
        PATCH (update_draft) must not insert rows into PayItems,
        CdpiDefinitions, BranchPayItemConfig, PayItemSettings,
        RateTypes, or PayItemRateTypeMap.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        body = CdpiRequestCreate(requesting_branch_id=hq_id)
        created = await cdpi_service.create_draft(company_id, admin_id, body, direct_db)
        before = await self._counts(direct_db)
        try:
            await cdpi_service.update_draft(
                company_id, admin_id, created.request_id,
                CdpiRequestUpdate(
                    expected_revision=1,
                    item_name="Updated",
                    input_type="Number",
                    calc_method_key="PerUnit",
                ),
                direct_db,
            )
            after = await self._counts(direct_db)
            for table, count in before.items():
                assert after[table] == count, (
                    f"update_draft unexpectedly inserted rows into {table}"
                )
        finally:
            await _cleanup_requests(direct_db, created.request_id)
