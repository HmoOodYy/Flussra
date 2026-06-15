"""
test_cdpi_schema.py

Focused schema-level tests for the CDPI (Custom Daily Pay Item) foundation
tables added in migrations 0043 and 0044.

All tests use direct database connections (bypassing the HTTP API) to verify
database-enforced constraints: CHECK constraints, FK integrity (including
composite company-scoped FKs), the append-only event trigger, and NOT NULL
audit fields.

Fixtures:
    direct_db  -- function-scoped AUTOCOMMIT AsyncConnection (SQLAlchemy asyncpg)
"""
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _demo_ids(db):
    """Return (company_id, branch_id, user_id) for the seeded DEMO company."""
    company_id = (await db.execute(
        _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO'")
    )).scalar_one()
    branch_id = (await db.execute(
        _text("SELECT branchid FROM core.branches WHERE branchcode = 'HQ'")
    )).scalar_one()
    user_id = (await db.execute(
        _text("SELECT userid FROM sec.users WHERE username = 'admin'")
    )).scalar_one()
    return company_id, branch_id, user_id


async def _insert_request(db, *, company_id, branch_id, user_id,
                           status="Draft", approved_item_id=None,
                           copied_from_id=None, revision=1):
    """Insert a CdpiRequest row and return its RequestID."""
    result = await db.execute(
        _text("""
            INSERT INTO payroll.CdpiRequests
                (CompanyID, RequestingBranchID, Status, Revision,
                 ApprovedPayItemID, CopiedFromRequestID,
                 CreatedByUserID, CreatedAtUtc)
            VALUES
                (:cid, :bid, :status, :rev,
                 :approved_item, :copied_from,
                 :uid, NOW())
            RETURNING RequestID
        """),
        {
            "cid": company_id, "bid": branch_id, "status": status,
            "rev": revision, "approved_item": approved_item_id,
            "copied_from": copied_from_id, "uid": user_id,
        },
    )
    return result.scalar_one()


async def _insert_event(db, *, request_id, event_type, to_status, actor_user_id,
                         from_status=None, revision=1):
    """Insert a CdpiRequestEvent row and return its EventID."""
    result = await db.execute(
        _text("""
            INSERT INTO payroll.CdpiRequestEvents
                (RequestID, EventType, FromStatus, ToStatus,
                 ActorUserID, RequestRevision, OccurredAtUtc)
            VALUES
                (:rid, :etype, :fstatus, :tstatus, :uid, :rev, NOW())
            RETURNING EventID
        """),
        {
            "rid": request_id, "etype": event_type,
            "fstatus": from_status, "tstatus": to_status,
            "uid": actor_user_id, "rev": revision,
        },
    )
    return result.scalar_one()


async def _insert_second_company(db):
    """Insert a second company and branch for cross-tenant tests.  Returns (company_id, branch_id)."""
    co_id = (await db.execute(
        _text("""
            INSERT INTO core.companies
                (companycode, companyname, legalname, status, issuspended, timezonename)
            VALUES ('CDPI_TEST_CO', 'CDPI Test Co', 'CDPI Test Co Ltd',
                    'Active', FALSE, 'UTC')
            RETURNING companyid
        """)
    )).scalar_one()
    br_id = (await db.execute(
        _text("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
            VALUES (:cid, 'CDPI_BR', 'CDPI Branch', 'Active', TRUE)
            RETURNING branchid
        """),
        {"cid": co_id},
    )).scalar_one()
    return co_id, br_id


async def _cleanup_second_company(db, company_id):
    """Remove the ephemeral second company and all dependent rows."""
    await db.execute(
        _text("DELETE FROM core.branches WHERE companyid = :cid"), {"cid": company_id}
    )
    await db.execute(
        _text("DELETE FROM core.companies WHERE companyid = :cid"), {"cid": company_id}
    )


async def _insert_custom_pay_item(db, *, company_id, user_id):
    """Insert a minimal custom PayItems row and return its PayItemID."""
    code = f"CDPI_{uuid.uuid4().hex[:8].upper()}"
    return (await db.execute(
        _text("""
            INSERT INTO payroll.PayItems
                (CompanyID, PayItemCode, PayItemName, Category, DataType, Status,
                 ItemScope, RateBehavior, IsDefaultBranchActive, IsSystemStandard,
                 SortOrder, AppearsInPayrollEntry, AppearsInLedger, AppearsInReports,
                 RequiresRate, CreatedByUserID, CreatedAtUtc)
            VALUES
                (:cid, :code, 'CDPI Test Item', 'Custom', 'Decimal',
                 'Active', 'Daily', 'PerUnit', FALSE, FALSE, 999, TRUE, TRUE, TRUE,
                 FALSE, :uid, NOW())
            RETURNING PayItemID
        """),
        {"cid": company_id, "uid": user_id, "code": code},
    )).scalar_one()


async def _cleanup_custom_pay_item(db, item_id):
    await db.execute(
        _text("DELETE FROM payroll.PayItems WHERE payitemid = :iid"), {"iid": item_id}
    )


async def _cleanup_cdpi_requests(db, *request_ids):
    for rid in request_ids:
        # Temporarily disable the immutability trigger so cleanup can remove events.
        await db.execute(
            _text("ALTER TABLE payroll.CdpiRequestEvents DISABLE TRIGGER ALL")
        )
        try:
            await db.execute(
                _text("DELETE FROM payroll.CdpiRequestEvents WHERE requestid = :rid"),
                {"rid": rid},
            )
        finally:
            await db.execute(
                _text("ALTER TABLE payroll.CdpiRequestEvents ENABLE TRIGGER ALL")
            )
        await db.execute(
            _text("DELETE FROM payroll.CdpiRequests WHERE requestid = :rid"),
            {"rid": rid},
        )


# ---------------------------------------------------------------------------
# 1. UUID defaults
# ---------------------------------------------------------------------------

async def test_uuid_defaults_generate_distinct_non_null_ids(direct_db):
    cid, bid, uid = await _demo_ids(direct_db)
    rid1 = await _insert_request(direct_db, company_id=cid, branch_id=bid, user_id=uid)
    rid2 = await _insert_request(direct_db, company_id=cid, branch_id=bid, user_id=uid)
    try:
        assert rid1 is not None
        assert rid2 is not None
        assert rid1 != rid2
    finally:
        await _cleanup_cdpi_requests(direct_db, rid1, rid2)

    # Verify CdpiRequestEvents also generates distinct UUIDs
    rid3 = await _insert_request(direct_db, company_id=cid, branch_id=bid, user_id=uid)
    try:
        eid1 = await _insert_event(direct_db, request_id=rid3, event_type="DraftCreated",
                                   to_status="Draft", actor_user_id=uid)
        eid2 = await _insert_event(direct_db, request_id=rid3, event_type="Submitted",
                                   from_status="Draft", to_status="PendingCompanyApproval",
                                   actor_user_id=uid, revision=2)
        assert eid1 is not None
        assert eid2 is not None
        assert eid1 != eid2
    finally:
        await _cleanup_cdpi_requests(direct_db, rid3)


# ---------------------------------------------------------------------------
# 2. Tenant integrity -- branch cross-company
# ---------------------------------------------------------------------------

async def test_request_branch_cross_company_rejected(direct_db):
    """CdpiRequests: branch from a different company is rejected by composite FK."""
    cid, _, uid = await _demo_ids(direct_db)
    other_cid, other_bid = await _insert_second_company(direct_db)
    try:
        with pytest.raises(IntegrityError):
            await _insert_request(
                direct_db,
                company_id=cid,        # DEMO company
                branch_id=other_bid,   # branch belongs to other company
                user_id=uid,
            )
    finally:
        await _cleanup_second_company(direct_db, other_cid)


# ---------------------------------------------------------------------------
# 3. Tenant integrity -- approved Pay Item cross-company
# ---------------------------------------------------------------------------

async def test_approved_item_cross_company_rejected(direct_db):
    """ApprovedPayItemID from a different company is rejected by composite FK."""
    cid, bid, uid = await _demo_ids(direct_db)
    other_cid, _ = await _insert_second_company(direct_db)
    other_item_id = await _insert_custom_pay_item(
        direct_db, company_id=other_cid, user_id=uid
    )
    demo_item_id = await _insert_custom_pay_item(direct_db, company_id=cid, user_id=uid)
    try:
        # Attempt: DEMO request approved with other company's item (wrong company)
        with pytest.raises(IntegrityError):
            await _insert_request(
                direct_db,
                company_id=cid,
                branch_id=bid,
                user_id=uid,
                status="Approved",
                approved_item_id=other_item_id,  # belongs to other company
            )
    finally:
        await _cleanup_custom_pay_item(direct_db, demo_item_id)
        await _cleanup_custom_pay_item(direct_db, other_item_id)
        await _cleanup_second_company(direct_db, other_cid)


# ---------------------------------------------------------------------------
# 4. Lifecycle integrity
# ---------------------------------------------------------------------------

async def test_approved_status_requires_approved_pay_item_id(direct_db):
    """Status='Approved' with NULL ApprovedPayItemID violates lifecycle CHECK."""
    cid, bid, uid = await _demo_ids(direct_db)
    with pytest.raises(IntegrityError):
        await _insert_request(
            direct_db,
            company_id=cid, branch_id=bid, user_id=uid,
            status="Approved",
            approved_item_id=None,  # must be non-null when Approved
        )


async def test_draft_status_must_have_null_approved_item(direct_db):
    """Status='Draft' with non-null ApprovedPayItemID violates lifecycle CHECK."""
    cid, bid, uid = await _demo_ids(direct_db)
    with pytest.raises(IntegrityError):
        await _insert_request(
            direct_db,
            company_id=cid, branch_id=bid, user_id=uid,
            status="Draft",
            approved_item_id=999999,  # non-null violates lifecycle
        )


async def test_pending_status_must_have_null_approved_item(direct_db):
    """Status='PendingCompanyApproval' with non-null item violates lifecycle CHECK."""
    cid, bid, uid = await _demo_ids(direct_db)
    with pytest.raises(IntegrityError):
        await _insert_request(
            direct_db,
            company_id=cid, branch_id=bid, user_id=uid,
            status="PendingCompanyApproval",
            approved_item_id=999999,
        )


async def test_rejected_status_must_have_null_approved_item(direct_db):
    """Status='Rejected' with non-null item violates lifecycle CHECK."""
    cid, bid, uid = await _demo_ids(direct_db)
    with pytest.raises(IntegrityError):
        await _insert_request(
            direct_db,
            company_id=cid, branch_id=bid, user_id=uid,
            status="Rejected",
            approved_item_id=999999,
        )


# ---------------------------------------------------------------------------
# 5. Positive numeric constraints
# ---------------------------------------------------------------------------

async def test_request_revision_zero_rejected(direct_db):
    cid, bid, uid = await _demo_ids(direct_db)
    with pytest.raises(IntegrityError):
        await _insert_request(
            direct_db, company_id=cid, branch_id=bid, user_id=uid, revision=0
        )


async def test_request_revision_negative_rejected(direct_db):
    cid, bid, uid = await _demo_ids(direct_db)
    with pytest.raises(IntegrityError):
        await _insert_request(
            direct_db, company_id=cid, branch_id=bid, user_id=uid, revision=-1
        )


async def test_event_revision_zero_rejected(direct_db):
    cid, bid, uid = await _demo_ids(direct_db)
    rid = await _insert_request(direct_db, company_id=cid, branch_id=bid, user_id=uid)
    try:
        with pytest.raises(IntegrityError):
            await _insert_event(
                direct_db, request_id=rid, event_type="DraftCreated",
                to_status="Draft", actor_user_id=uid, revision=0
            )
    finally:
        await _cleanup_cdpi_requests(direct_db, rid)


async def test_event_revision_negative_rejected(direct_db):
    cid, bid, uid = await _demo_ids(direct_db)
    rid = await _insert_request(direct_db, company_id=cid, branch_id=bid, user_id=uid)
    try:
        with pytest.raises(IntegrityError):
            await _insert_event(
                direct_db, request_id=rid, event_type="DraftCreated",
                to_status="Draft", actor_user_id=uid, revision=-5
            )
    finally:
        await _cleanup_cdpi_requests(direct_db, rid)


async def test_definition_schema_version_zero_rejected(direct_db):
    cid, _, uid = await _demo_ids(direct_db)
    item_id = await _insert_custom_pay_item(direct_db, company_id=cid, user_id=uid)
    try:
        with pytest.raises(IntegrityError):
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.CdpiDefinitions
                        (PayItemID, DefinitionSchemaVersion, LockedAtUtc, CreatedByUserID, CreatedAtUtc)
                    VALUES (:iid, 0, NOW(), :uid, NOW())
                """),
                {"iid": item_id, "uid": uid},
            )
    finally:
        await _cleanup_custom_pay_item(direct_db, item_id)


async def test_definition_schema_version_negative_rejected(direct_db):
    cid, _, uid = await _demo_ids(direct_db)
    item_id = await _insert_custom_pay_item(direct_db, company_id=cid, user_id=uid)
    try:
        with pytest.raises(IntegrityError):
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.CdpiDefinitions
                        (PayItemID, DefinitionSchemaVersion, LockedAtUtc, CreatedByUserID, CreatedAtUtc)
                    VALUES (:iid, -1, NOW(), :uid, NOW())
                """),
                {"iid": item_id, "uid": uid},
            )
    finally:
        await _cleanup_custom_pay_item(direct_db, item_id)


# ---------------------------------------------------------------------------
# 6. Required creation audit actor
# ---------------------------------------------------------------------------

async def test_request_created_by_required(direct_db):
    """CdpiRequests.CreatedByUserID is NOT NULL after migration 0044."""
    cid, bid, _ = await _demo_ids(direct_db)
    with pytest.raises(IntegrityError):
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.CdpiRequests
                    (CompanyID, RequestingBranchID, Status, Revision, CreatedAtUtc)
                VALUES (:cid, :bid, 'Draft', 1, NOW())
            """),
            {"cid": cid, "bid": bid},
        )


async def test_definition_created_by_required(direct_db):
    """CdpiDefinitions.CreatedByUserID is NOT NULL after migration 0044."""
    cid, _, uid = await _demo_ids(direct_db)
    item_id = await _insert_custom_pay_item(direct_db, company_id=cid, user_id=uid)
    try:
        with pytest.raises(IntegrityError):
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.CdpiDefinitions
                        (PayItemID, DefinitionSchemaVersion, LockedAtUtc, CreatedAtUtc)
                    VALUES (:iid, 1, NOW(), NOW())
                """),
                {"iid": item_id},
            )
    finally:
        await _cleanup_custom_pay_item(direct_db, item_id)


# ---------------------------------------------------------------------------
# 7. CopiedFromRequestID FK and cross-company constraint
# ---------------------------------------------------------------------------

async def test_copied_from_must_reference_existing_request(direct_db):
    """CopiedFromRequestID referencing a non-existent RequestID is rejected."""
    cid, bid, uid = await _demo_ids(direct_db)
    fake_uuid = uuid.uuid4()
    with pytest.raises(IntegrityError):
        await _insert_request(
            direct_db, company_id=cid, branch_id=bid, user_id=uid,
            copied_from_id=fake_uuid,
        )


async def test_copied_from_cross_company_rejected(direct_db):
    """CopiedFromRequestID from a different company is rejected by composite FK."""
    cid, bid, uid = await _demo_ids(direct_db)
    other_cid, other_bid = await _insert_second_company(direct_db)
    other_rid = await _insert_request(
        direct_db, company_id=other_cid, branch_id=other_bid, user_id=uid
    )
    try:
        # DEMO request claiming to copy from the other company's request
        with pytest.raises(IntegrityError):
            await _insert_request(
                direct_db, company_id=cid, branch_id=bid, user_id=uid,
                copied_from_id=other_rid,  # belongs to other company
            )
    finally:
        await direct_db.execute(
            _text("DELETE FROM payroll.CdpiRequests WHERE requestid = :rid"),
            {"rid": other_rid},
        )
        await _cleanup_second_company(direct_db, other_cid)


# ---------------------------------------------------------------------------
# 8. Append-only event trigger
# ---------------------------------------------------------------------------

async def test_event_update_rejected_by_trigger(direct_db):
    """UPDATE on CdpiRequestEvents is blocked by the immutability trigger."""
    cid, bid, uid = await _demo_ids(direct_db)
    rid = await _insert_request(direct_db, company_id=cid, branch_id=bid, user_id=uid)
    try:
        eid = await _insert_event(
            direct_db, request_id=rid, event_type="DraftCreated",
            to_status="Draft", actor_user_id=uid,
        )
        with pytest.raises(IntegrityError) as exc_info:
            await direct_db.execute(
                _text("UPDATE payroll.CdpiRequestEvents SET Reason = 'tampered' "
                      "WHERE eventid = :eid"),
                {"eid": eid},
            )
        assert "cdpi_events_immutable" in str(exc_info.value).lower()
    finally:
        await _cleanup_cdpi_requests(direct_db, rid)


async def test_event_delete_rejected_by_trigger(direct_db):
    """DELETE on CdpiRequestEvents is blocked by the immutability trigger."""
    cid, bid, uid = await _demo_ids(direct_db)
    rid = await _insert_request(direct_db, company_id=cid, branch_id=bid, user_id=uid)
    try:
        eid = await _insert_event(
            direct_db, request_id=rid, event_type="DraftCreated",
            to_status="Draft", actor_user_id=uid,
        )
        with pytest.raises(IntegrityError) as exc_info:
            await direct_db.execute(
                _text("DELETE FROM payroll.CdpiRequestEvents WHERE eventid = :eid"),
                {"eid": eid},
            )
        assert "cdpi_events_immutable" in str(exc_info.value).lower()
    finally:
        await _cleanup_cdpi_requests(direct_db, rid)


async def test_trigger_exists_in_test_schema(direct_db):
    """Verify the immutability trigger is present in the test-database schema."""
    count = (await direct_db.execute(
        _text("""
            SELECT COUNT(*) FROM information_schema.triggers
            WHERE trigger_schema = 'payroll'
              AND event_object_table = 'cdpirequestevents'
              AND trigger_name = 'trg_guard_cdpi_request_events_immutable'
        """)
    )).scalar_one()
    # BEFORE UPDATE and BEFORE DELETE are two rows in information_schema.triggers
    assert count == 2, (
        f"Expected 2 trigger rows (BEFORE UPDATE + BEFORE DELETE) for "
        f"trg_guard_cdpi_request_events_immutable, got {count}"
    )


# ---------------------------------------------------------------------------
# 9. One-to-one approved-definition relationship
# ---------------------------------------------------------------------------

async def test_definition_is_one_to_one(direct_db):
    """Two CdpiDefinitions rows for the same PayItemID are rejected (PK)."""
    cid, _, uid = await _demo_ids(direct_db)
    item_id = await _insert_custom_pay_item(direct_db, company_id=cid, user_id=uid)
    try:
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.CdpiDefinitions
                    (PayItemID, DefinitionSchemaVersion, LockedAtUtc, CreatedByUserID, CreatedAtUtc)
                VALUES (:iid, 1, NOW(), :uid, NOW())
            """),
            {"iid": item_id, "uid": uid},
        )
        with pytest.raises(IntegrityError):
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.CdpiDefinitions
                        (PayItemID, DefinitionSchemaVersion, LockedAtUtc, CreatedByUserID, CreatedAtUtc)
                    VALUES (:iid, 1, NOW(), :uid, NOW())
                """),
                {"iid": item_id, "uid": uid},
            )
    finally:
        await direct_db.execute(
            _text("DELETE FROM payroll.CdpiDefinitions WHERE payitemid = :iid"),
            {"iid": item_id},
        )
        await _cleanup_custom_pay_item(direct_db, item_id)


async def test_definition_has_no_source_request_column(direct_db):
    """CdpiDefinitions.SourceRequestID column was removed in migration 0044."""
    row = (await direct_db.execute(
        _text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'payroll'
              AND table_name = 'cdpidefinitions'
              AND column_name = 'sourcerequestid'
        """)
    )).fetchone()
    assert row is None, (
        "SourceRequestID column still exists in CdpiDefinitions -- "
        "migration 0044 should have removed it"
    )


async def test_approved_request_links_to_unique_pay_item(direct_db):
    """Two Approved requests cannot reference the same ApprovedPayItemID (UNIQUE)."""
    cid, bid, uid = await _demo_ids(direct_db)
    item_id = await _insert_custom_pay_item(direct_db, company_id=cid, user_id=uid)
    try:
        rid1 = await _insert_request(
            direct_db, company_id=cid, branch_id=bid, user_id=uid,
            status="Approved", approved_item_id=item_id,
        )
        try:
            with pytest.raises(IntegrityError):
                await _insert_request(
                    direct_db, company_id=cid, branch_id=bid, user_id=uid,
                    status="Approved", approved_item_id=item_id,  # duplicate
                )
        finally:
            await _cleanup_cdpi_requests(direct_db, rid1)
    finally:
        await _cleanup_custom_pay_item(direct_db, item_id)
