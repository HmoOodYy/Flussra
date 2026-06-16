"""
test_cdpi_workflow.py

Task 4 integration tests: Submit / Return / Reject / Copy for CDPI requests.

Part A -- schema/contract (sync, no DB):
  TestCdpiSubmitRequest, TestCdpiDecideRequest

Part B -- service integration (async, direct_db):
  TestSubmitDraft, TestReturnToDraft, TestRejectRequest, TestCopyRejected,
  TestScopeAndSafety
"""
import uuid
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text as _text

from app.cdpi.schemas import (
    CdpiRequestCreate,
    CdpiRequestUpdate,
    CdpiSubmitRequest,
    CdpiDecideRequest,
    CdpiDecideAction,
)
from app.cdpi import service as cdpi_service


# ===========================================================================
# Shared DB helpers (same pattern as test_cdpi_draft.py)
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
                        scope: str = "AllCompanyBranches", branch_id=None) -> None:
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


async def _create_complete_draft(db, company_id, branch_id, user_id,
                                  calc_method="PerUnit") -> object:
    """Create a Draft with all fields required for submission."""
    return await cdpi_service.create_draft(
        company_id, user_id,
        CdpiRequestCreate(
            requesting_branch_id=branch_id,
            item_name="Night Allowance",
            input_type="Number",
            calc_method_key=calc_method,
        ),
        db,
    )


async def _submit(db, company_id, user_id, request_id, revision=1):
    return await cdpi_service.submit_draft(
        company_id, user_id, request_id,
        CdpiSubmitRequest(expected_revision=revision),
        db,
    )


async def _decide(db, company_id, user_id, request_id, action, revision,
                   reason="Test reason"):
    return await cdpi_service.decide_request(
        company_id, user_id, request_id,
        CdpiDecideRequest(
            action=action,
            expected_revision=revision,
            reason=reason,
        ),
        db,
    )


async def _event_types(db, request_id) -> list:
    rows = (await db.execute(
        _text("""
            SELECT eventtype FROM payroll.cdpirequestevents
            WHERE requestid = :rid
            ORDER BY occurredatutc
        """),
        {"rid": str(request_id)},
    )).all()
    return [r[0] for r in rows]


async def _legacy_counts(db) -> dict:
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


# ===========================================================================
# Part A -- schema/contract (sync, no DB)
# ===========================================================================

class TestCdpiSubmitRequest:
    def test_requires_expected_revision(self):
        with pytest.raises(ValidationError):
            CdpiSubmitRequest()

    def test_valid(self):
        body = CdpiSubmitRequest(expected_revision=3)
        assert body.expected_revision == 3


class TestCdpiDecideRequest:
    def test_requires_all_fields(self):
        with pytest.raises(ValidationError):
            CdpiDecideRequest(action="ReturnToDraft", expected_revision=1)

    def test_empty_reason_rejected(self):
        with pytest.raises(ValidationError):
            CdpiDecideRequest(
                action="ReturnToDraft", expected_revision=1, reason=""
            )

    def test_whitespace_only_reason_rejected(self):
        with pytest.raises(ValidationError):
            CdpiDecideRequest(
                action="ReturnToDraft", expected_revision=1, reason="   "
            )

    def test_approve_action_is_valid_in_schema(self):
        """Approve was added in Task 6; it must now be accepted by the schema."""
        body = CdpiDecideRequest(action="Approve", expected_revision=1, reason="ok")
        assert body.action == CdpiDecideAction.Approve

    def test_valid_return_to_draft(self):
        body = CdpiDecideRequest(
            action="ReturnToDraft", expected_revision=2, reason="Fix name"
        )
        assert body.action == CdpiDecideAction.ReturnToDraft
        assert body.reason == "Fix name"

    def test_valid_reject(self):
        body = CdpiDecideRequest(
            action="Reject", expected_revision=2, reason="Duplicate"
        )
        assert body.action == CdpiDecideAction.Reject


# ===========================================================================
# Part B -- service integration (async, direct_db)
# ===========================================================================

@pytest.mark.asyncio
class TestSubmitDraft:
    async def test_submit_transitions_status(self, direct_db):
        """Branch user submits a complete PerUnit Draft; status becomes PendingCompanyApproval."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            result = await _submit(direct_db, company_id, admin_id, draft.request_id)
            assert result.status == "PendingCompanyApproval"
            assert result.submitted_by_user_id == admin_id
            assert result.submitted_at_utc is not None
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_writes_submitted_event_on_first_submit(self, direct_db):
        """First submission writes exactly one Submitted event."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            await _submit(direct_db, company_id, admin_id, draft.request_id)
            events = await _event_types(direct_db, draft.request_id)
            assert events.count("Submitted") == 1
            assert events.count("Resubmitted") == 0
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_resubmit_after_return_writes_resubmitted_event(self, direct_db):
        """After a ReturnToDraft, submitting again writes Resubmitted."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            returned = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.ReturnToDraft, pending.revision
            )
            await _submit(
                direct_db, company_id, admin_id, draft.request_id,
                revision=returned.revision
            )
            events = await _event_types(direct_db, draft.request_id)
            assert "Resubmitted" in events
            assert events.index("Resubmitted") > events.index("Submitted")
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_increments_revision(self, direct_db):
        """Submit increments Revision by exactly 1."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            assert draft.revision == 1
            result = await _submit(direct_db, company_id, admin_id, draft.request_id)
            assert result.revision == 2
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_requires_expected_revision(self, direct_db):
        """Submit with stale revision raises 409 and leaves row unchanged."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.submit_draft(
                    company_id, admin_id, draft.request_id,
                    CdpiSubmitRequest(expected_revision=99),
                    direct_db,
                )
            assert exc_info.value.status_code == 409
            # Row must be unchanged.
            row = await cdpi_service.get_request(
                company_id, admin_id, draft.request_id, direct_db
            )
            assert row.status == "Draft"
            assert row.revision == 1
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_rejects_non_draft(self, direct_db):
        """Submitting an already-submitted (Pending) request raises 422."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            with pytest.raises(HTTPException) as exc_info:
                await _submit(
                    direct_db, company_id, admin_id, draft.request_id,
                    revision=pending.revision
                )
            assert exc_info.value.status_code == 422
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_rejects_missing_item_name(self, direct_db):
        """Submit rejects a Draft with no ItemName."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                input_type="Number",
                calc_method_key="PerUnit",
                # item_name omitted
            ),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _submit(direct_db, company_id, admin_id, draft.request_id)
            assert exc_info.value.status_code == 422
            assert "ItemName" in exc_info.value.detail
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_rejects_missing_input_type(self, direct_db):
        """Submit rejects a Draft with no InputType."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Test",
                calc_method_key="PerUnit",
                # input_type omitted
            ),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _submit(direct_db, company_id, admin_id, draft.request_id)
            assert exc_info.value.status_code == 422
            assert "InputType" in exc_info.value.detail
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_rejects_missing_calc_method_key(self, direct_db):
        """Submit rejects a Draft with no CalcMethodKey."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Test",
                input_type="Number",
                # calc_method_key omitted
            ),
            direct_db,
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _submit(direct_db, company_id, admin_id, draft.request_id)
            assert exc_info.value.status_code == 422
            assert "CalcMethodKey" in exc_info.value.detail
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_accepts_null_unit(self, direct_db):
        """Submit succeeds when Unit is omitted; unit is optional."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Bonus",
                input_type="Number",
                calc_method_key="PerUnit",
                # unit omitted
            ),
            direct_db,
        )
        try:
            result = await _submit(direct_db, company_id, admin_id, draft.request_id)
            assert result.status == "PendingCompanyApproval"
            assert result.unit is None
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_rejects_non_per_unit_method(self, direct_db):
        """Submit rejects OrdinalTier, Block, etc. with a clean unsupported-method 422."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        for method in ("OrdinalTier", "Block", "RangeBracket", "RangeProgressive"):
            draft = await _create_complete_draft(
                direct_db, company_id, hq_id, admin_id, calc_method=method
            )
            try:
                with pytest.raises(HTTPException) as exc_info:
                    await _submit(direct_db, company_id, admin_id, draft.request_id)
                assert exc_info.value.status_code == 422
                assert "PerUnit" in exc_info.value.detail
            finally:
                await _cleanup_requests(direct_db, draft.request_id)

    async def test_submit_creates_no_legacy_rows(self, direct_db):
        """Submit must not touch PayItems, CdpiDefinitions, or any legacy table."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        before = await _legacy_counts(direct_db)
        try:
            await _submit(direct_db, company_id, admin_id, draft.request_id)
            after = await _legacy_counts(direct_db)
            for table, count in before.items():
                assert after[table] == count, f"submit created rows in {table}"
        finally:
            await _cleanup_requests(direct_db, draft.request_id)


@pytest.mark.asyncio
class TestReturnToDraft:
    async def test_company_user_can_return_pending_request(self, direct_db):
        """AllCompanyBranches reviewer can return PendingCompanyApproval -> Draft."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            returned = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.ReturnToDraft, pending.revision,
                reason="Please correct the item name."
            )
            assert returned.status == "Draft"
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_branch_scoped_user_cannot_return(self, direct_db):
        """SpecificBranch user cannot return a Pending request (403)."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        branch_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"buser_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db, company_id=company_id,
            role_code=f"BSCOPE_{suffix}", perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db, user_id=branch_user_id, company_id=company_id,
            role_id=role_id, scope="SpecificBranch", branch_id=hq_id,
        )
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            with pytest.raises(HTTPException) as exc_info:
                await _decide(
                    direct_db, company_id, branch_user_id, draft.request_id,
                    CdpiDecideAction.ReturnToDraft, pending.revision
                )
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, draft.request_id)
            await _cleanup_user(direct_db, branch_user_id)
            await _cleanup_role(direct_db, role_id)

    async def test_return_requires_non_empty_reason(self, direct_db):
        """Reason is enforced at the schema level."""
        with pytest.raises(ValidationError):
            CdpiDecideRequest(
                action="ReturnToDraft", expected_revision=1, reason="  "
            )

    async def test_return_writes_returned_to_draft_event(self, direct_db):
        """Return writes a ReturnedToDraft event with the provided reason."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.ReturnToDraft, pending.revision,
                reason="Fix item name"
            )
            events = await _event_types(direct_db, draft.request_id)
            assert "ReturnedToDraft" in events

            reason_row = (await direct_db.execute(
                _text("""
                    SELECT reason FROM payroll.cdpirequestevents
                    WHERE requestid = :rid AND eventtype = 'ReturnedToDraft'
                """),
                {"rid": str(draft.request_id)},
            )).mappings().first()
            assert reason_row["reason"] == "Fix item name"
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_return_increments_revision(self, direct_db):
        """Return increments Revision by exactly 1."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            assert pending.revision == 2
            returned = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.ReturnToDraft, pending.revision
            )
            assert returned.revision == 3
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_return_stale_revision_raises_409(self, direct_db):
        """Return with wrong revision raises 409 and leaves row unchanged."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            with pytest.raises(HTTPException) as exc_info:
                await _decide(
                    direct_db, company_id, admin_id, draft.request_id,
                    CdpiDecideAction.ReturnToDraft, revision=99
                )
            assert exc_info.value.status_code == 409
            row = await cdpi_service.get_request(
                company_id, admin_id, draft.request_id, direct_db
            )
            assert row.status == "PendingCompanyApproval"
            assert row.revision == pending.revision
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_return_does_not_modify_definition_fields(self, direct_db):
        """Return must not change ItemName, InputType, CalcMethodKey, or Notes."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await cdpi_service.create_draft(
            company_id, admin_id,
            CdpiRequestCreate(
                requesting_branch_id=hq_id,
                item_name="Preserved Name",
                input_type="Time",
                calc_method_key="PerUnit",
                notes="Keep this",
            ),
            direct_db,
        )
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            returned = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.ReturnToDraft, pending.revision
            )
            assert returned.item_name == "Preserved Name"
            assert returned.input_type == "Time"
            assert returned.calc_method_key == "PerUnit"
            assert returned.notes == "Keep this"
        finally:
            await _cleanup_requests(direct_db, draft.request_id)


@pytest.mark.asyncio
class TestRejectRequest:
    async def test_company_user_can_reject(self, direct_db):
        """AllCompanyBranches reviewer can reject a PendingCompanyApproval request."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            rejected = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.Reject, pending.revision, reason="Duplicate item"
            )
            assert rejected.status == "Rejected"
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_branch_scoped_user_cannot_reject(self, direct_db):
        """SpecificBranch user cannot reject (403)."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        branch_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"brej_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db, company_id=company_id,
            role_code=f"BREJ_{suffix}", perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db, user_id=branch_user_id, company_id=company_id,
            role_id=role_id, scope="SpecificBranch", branch_id=hq_id,
        )
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            with pytest.raises(HTTPException) as exc_info:
                await _decide(
                    direct_db, company_id, branch_user_id, draft.request_id,
                    CdpiDecideAction.Reject, pending.revision
                )
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, draft.request_id)
            await _cleanup_user(direct_db, branch_user_id)
            await _cleanup_role(direct_db, role_id)

    async def test_reject_requires_non_empty_reason(self, direct_db):
        """Reason validation is at schema level."""
        with pytest.raises(ValidationError):
            CdpiDecideRequest(action="Reject", expected_revision=1, reason="")

    async def test_reject_writes_rejected_event_with_reason(self, direct_db):
        """Reject writes a Rejected event containing the supplied reason."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.Reject, pending.revision, reason="Policy conflict"
            )
            events = await _event_types(direct_db, draft.request_id)
            assert "Rejected" in events

            reason_row = (await direct_db.execute(
                _text("""
                    SELECT reason FROM payroll.cdpirequestevents
                    WHERE requestid = :rid AND eventtype = 'Rejected'
                """),
                {"rid": str(draft.request_id)},
            )).mappings().first()
            assert reason_row["reason"] == "Policy conflict"
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_reject_increments_revision(self, direct_db):
        """Reject increments Revision by exactly 1."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            rejected = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.Reject, pending.revision
            )
            assert rejected.revision == pending.revision + 1
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_rejected_request_cannot_be_submitted(self, direct_db):
        """A Rejected request raises 422 on any further submit attempt."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            rejected = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.Reject, pending.revision
            )
            with pytest.raises(HTTPException) as exc_info:
                await _submit(
                    direct_db, company_id, admin_id, draft.request_id,
                    revision=rejected.revision
                )
            assert exc_info.value.status_code == 422

            row = await cdpi_service.get_request(
                company_id, admin_id, draft.request_id, direct_db
            )
            assert row.status == "Rejected"
            assert row.revision == rejected.revision
        finally:
            await _cleanup_requests(direct_db, draft.request_id)


@pytest.mark.asyncio
class TestCopyRejected:
    async def _reject_request(self, db, company_id, admin_id, hq_id):
        """Helper: create, submit, and reject a request."""
        draft = await _create_complete_draft(db, company_id, hq_id, admin_id)
        pending = await _submit(db, company_id, admin_id, draft.request_id)
        rejected = await _decide(
            db, company_id, admin_id, draft.request_id,
            CdpiDecideAction.Reject, pending.revision
        )
        return rejected

    async def test_copy_creates_new_draft(self, direct_db):
        """Copy of a Rejected request creates a new Draft with Revision=1."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        original = await self._reject_request(direct_db, company_id, admin_id, hq_id)
        new_req = await cdpi_service.copy_rejected(
            company_id, admin_id, original.request_id, direct_db
        )
        try:
            assert new_req.request_id != original.request_id
            assert new_req.status == "Draft"
            assert new_req.revision == 1
        finally:
            await _cleanup_requests(direct_db, new_req.request_id, original.request_id)

    async def test_copy_sets_copied_from_request_id(self, direct_db):
        """New Draft has CopiedFromRequestID pointing to the original."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        original = await self._reject_request(direct_db, company_id, admin_id, hq_id)
        new_req = await cdpi_service.copy_rejected(
            company_id, admin_id, original.request_id, direct_db
        )
        try:
            assert new_req.copied_from_request_id == original.request_id
        finally:
            await _cleanup_requests(direct_db, new_req.request_id, original.request_id)

    async def test_copy_writes_copied_from_rejected_event(self, direct_db):
        """New Draft has a CopiedFromRejected event."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        original = await self._reject_request(direct_db, company_id, admin_id, hq_id)
        new_req = await cdpi_service.copy_rejected(
            company_id, admin_id, original.request_id, direct_db
        )
        try:
            events = await _event_types(direct_db, new_req.request_id)
            assert events == ["CopiedFromRejected"]
        finally:
            await _cleanup_requests(direct_db, new_req.request_id, original.request_id)

    async def test_copy_original_remains_unchanged(self, direct_db):
        """Original Rejected request status and revision must not change after copy."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        original = await self._reject_request(direct_db, company_id, admin_id, hq_id)
        original_revision = original.revision
        new_req = await cdpi_service.copy_rejected(
            company_id, admin_id, original.request_id, direct_db
        )
        try:
            after = await cdpi_service.get_request(
                company_id, admin_id, original.request_id, direct_db
            )
            assert after.status == "Rejected"
            assert after.revision == original_revision
        finally:
            await _cleanup_requests(direct_db, new_req.request_id, original.request_id)

    async def test_copy_non_rejected_same_branch_raises_422(self, direct_db):
        """
        Same-branch user copying a Draft (non-Rejected) gets the expected 422.
        Permission passes first because they own the branch; the wrong-status
        error is correct and non-leaking.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.copy_rejected(
                    company_id, admin_id, draft.request_id, direct_db
                )
            assert exc_info.value.status_code == 422
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_copy_same_branch_rejected_succeeds(self, direct_db):
        """Same-branch user can still copy a Rejected request."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        original = await self._reject_request(direct_db, company_id, admin_id, hq_id)
        new_req = await cdpi_service.copy_rejected(
            company_id, admin_id, original.request_id, direct_db
        )
        try:
            assert new_req.status == "Draft"
        finally:
            await _cleanup_requests(direct_db, new_req.request_id, original.request_id)

    async def test_cross_branch_copy_of_draft_is_403_not_422(self, direct_db):
        """
        State-leak regression: a branch-scoped user copying another branch's
        Draft must be denied by permission (403), not status error (422).
        Without the fix, the old order returned 422 revealing the request status.
        """
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        hq_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"lkd_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db, company_id=company_id,
            role_code=f"LKD_{suffix}", perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db, user_id=hq_user_id, company_id=company_id,
            role_id=role_id, scope="SpecificBranch", branch_id=hq_id,
        )
        # Create a Draft on PAYTEST (not the user's branch).
        draft = await _create_complete_draft(
            direct_db, company_id, paytest_id, admin_id
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.copy_rejected(
                    company_id, hq_user_id, draft.request_id, direct_db
                )
            # Must be 403 (permission denied), not 422 (wrong status leak).
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, draft.request_id)
            await _cleanup_user(direct_db, hq_user_id)
            await _cleanup_role(direct_db, role_id)

    async def test_cross_branch_copy_of_pending_is_403_not_422(self, direct_db):
        """
        State-leak regression: a branch-scoped user copying another branch's
        PendingCompanyApproval request must receive 403, not 422.
        """
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        hq_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"lkp_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db, company_id=company_id,
            role_code=f"LKP_{suffix}", perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db, user_id=hq_user_id, company_id=company_id,
            role_id=role_id, scope="SpecificBranch", branch_id=hq_id,
        )
        draft = await _create_complete_draft(
            direct_db, company_id, paytest_id, admin_id
        )
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.copy_rejected(
                    company_id, hq_user_id, draft.request_id, direct_db
                )
            # Must be 403 (permission denied), not 422 (wrong status leak).
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, draft.request_id)
            await _cleanup_user(direct_db, hq_user_id)
            await _cleanup_role(direct_db, role_id)

    async def test_copy_creates_no_legacy_rows(self, direct_db):
        """Copy must not touch PayItems, CdpiDefinitions, or any legacy table."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        original = await self._reject_request(direct_db, company_id, admin_id, hq_id)
        before = await _legacy_counts(direct_db)
        new_req = await cdpi_service.copy_rejected(
            company_id, admin_id, original.request_id, direct_db
        )
        try:
            after = await _legacy_counts(direct_db)
            for table, count in before.items():
                assert after[table] == count, f"copy_rejected created rows in {table}"
        finally:
            await _cleanup_requests(direct_db, new_req.request_id, original.request_id)


@pytest.mark.asyncio
class TestScopeAndSafety:
    async def test_branch_user_cannot_submit_other_branch_request(self, direct_db):
        """Branch user scoped to HQ cannot submit a PAYTEST request."""
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        hq_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"hqonly_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db, company_id=company_id,
            role_code=f"HQONLY_{suffix}", perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db, user_id=hq_user_id, company_id=company_id,
            role_id=role_id, scope="SpecificBranch", branch_id=hq_id,
        )
        draft = await _create_complete_draft(
            direct_db, company_id, paytest_id, admin_id
        )
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _submit(
                    direct_db, company_id, hq_user_id, draft.request_id
                )
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, draft.request_id)
            await _cleanup_user(direct_db, hq_user_id)
            await _cleanup_role(direct_db, role_id)

    async def test_branch_user_cannot_copy_other_branch_request(self, direct_db):
        """Branch user scoped to HQ cannot copy a PAYTEST rejected request."""
        company_id, hq_id, paytest_id, admin_id = await _get_ids(direct_db)
        suffix = uuid.uuid4().hex[:6]
        hq_user_id = await _create_test_user(
            direct_db, company_id=company_id, username=f"hqcopy_{suffix}"
        )
        role_id = await _create_company_role(
            direct_db, company_id=company_id,
            role_code=f"HQCOPY_{suffix}", perms=["payitems.edit"],
        )
        await _assign_role(
            direct_db, user_id=hq_user_id, company_id=company_id,
            role_id=role_id, scope="SpecificBranch", branch_id=hq_id,
        )
        draft = await _create_complete_draft(
            direct_db, company_id, paytest_id, admin_id
        )
        try:
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            rejected = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.Reject, pending.revision
            )
            with pytest.raises(HTTPException) as exc_info:
                await cdpi_service.copy_rejected(
                    company_id, hq_user_id, rejected.request_id, direct_db
                )
            assert exc_info.value.status_code == 403
        finally:
            await _cleanup_requests(direct_db, draft.request_id)
            await _cleanup_user(direct_db, hq_user_id)
            await _cleanup_role(direct_db, role_id)

    async def test_company_user_can_read_and_list_after_transitions(self, direct_db):
        """AllCompanyBranches user sees the request at each lifecycle stage."""
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        try:
            # Draft -> Pending
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            fetched = await cdpi_service.get_request(
                company_id, admin_id, draft.request_id, direct_db
            )
            assert fetched.status == "PendingCompanyApproval"

            # Pending -> Rejected
            rejected = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.Reject, pending.revision
            )
            listed = await cdpi_service.list_requests(
                company_id, admin_id, direct_db, status_filter="Rejected"
            )
            assert draft.request_id in [r.request_id for r in listed]
        finally:
            await _cleanup_requests(direct_db, draft.request_id)

    async def test_no_task4_path_creates_payitems_or_cdpi_definitions(self, direct_db):
        """
        Complete submit->return->resubmit->reject->copy cycle must not create
        any PayItems or CdpiDefinitions rows.
        """
        company_id, hq_id, _, admin_id = await _get_ids(direct_db)
        before = await _legacy_counts(direct_db)
        draft = await _create_complete_draft(direct_db, company_id, hq_id, admin_id)
        new_req = None
        try:
            # submit
            pending = await _submit(direct_db, company_id, admin_id, draft.request_id)
            # return
            returned = await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.ReturnToDraft, pending.revision
            )
            # resubmit
            pending2 = await _submit(
                direct_db, company_id, admin_id, draft.request_id,
                revision=returned.revision
            )
            # reject
            await _decide(
                direct_db, company_id, admin_id, draft.request_id,
                CdpiDecideAction.Reject, pending2.revision
            )
            # copy
            new_req = await cdpi_service.copy_rejected(
                company_id, admin_id, draft.request_id, direct_db
            )
            after = await _legacy_counts(direct_db)
            for table, count in before.items():
                assert after[table] == count, (
                    f"Task 4 full cycle unexpectedly created rows in {table}"
                )
        finally:
            # Copy must be deleted before original (FK: CopiedFromRequestID).
            ids = []
            if new_req is not None:
                ids.append(new_req.request_id)
            ids.append(draft.request_id)
            await _cleanup_requests(direct_db, *ids)
