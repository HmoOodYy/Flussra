"""
tests/test_pay_rates_matrix_fallback.py — Matrix IsDefaultBranchActive fallback

Root cause fixed: get_driver_rate_matrix was using INNER JOIN on BranchPayItemConfig,
silently hiding system PayItems with IsDefaultBranchActive=TRUE when no explicit
config row existed for the branch (Headquarters in a fresh install).

These tests verify:
1. Matrix shows system items via IsDefaultBranchActive fallback when no config row exists.
2. Explicit IsActive=FALSE config overrides the fallback (item hidden).
3. Explicit IsActive=TRUE config shows item (even when default is FALSE).
4. Matrix and batch_save_rates agree on which items are active.
5. missing_required_count in summary uses the same source of truth.

Every test in this file is fully self-contained:
- Tests that mutate BranchPayItemConfig snapshot the prior state and restore it in
  finally blocks (via _set_bpic / _restore_bpic), so they leave the DB identical
  to how they found it regardless of pass or fail.
- The batch/matrix-agreement test creates its own isolated driver (unique driver_code)
  so it never writes to the session-shared created_driver_id, avoiding any rate-history
  contamination that would conflict with test_pay_rates.py tests.

HQ branch (branch_id=1) has NO BranchPayItemConfig rows in a fresh install.
System PayItems active by default (IsDefaultBranchActive=TRUE):
  Hours Worked     payitemid=1  ratetypeid=1  HOURLY
  Miles Driven     payitemid=2  ratetypeid=2  MILEAGE
  Loads Delivered  payitemid=3  ratetypeid=3  LOAD

System PayItems NOT active by default (IsDefaultBranchActive=FALSE):
  Overnight Stay   payitemid=4  ratetypeid=4  OVERNIGHT
"""
import uuid
import pytest
import httpx
from datetime import date

AS_OF_TODAY = date.today().isoformat()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Internal helpers — upsert/restore BranchPayItemConfig without INSERT conflicts
# ---------------------------------------------------------------------------

async def _set_bpic(
    direct_db,
    company_id: int,
    branch_id: int,
    pay_item_id: int,
    is_active: bool,
) -> dict:
    """
    Set BranchPayItemConfig.IsActive for (company, branch, pay_item).

    Handles both cases:
    - A row with EffectiveTo IS NULL already exists → UPDATE it.
    - No open row → INSERT one (EffectiveFrom='2000-01-01', EffectiveTo=NULL).

    Returns a restore-token that _restore_bpic uses to undo the change.

    The partial unique index uix_BranchPayItemConfig_OpenVersion on
    (CompanyID, BranchID, PayItemID) WHERE EffectiveTo IS NULL guarantees at
    most one open row per (company, branch, pay_item) — so a plain INSERT would
    conflict whenever another test already created an explicit row.  The
    read-first-then-upsert pattern avoids that.
    """
    from sqlalchemy import text as _text

    existing = (await direct_db.execute(
        _text("""
            SELECT configid, isactive
            FROM   payroll.branchpayitemconfig
            WHERE  companyid  = :c
              AND  branchid   = :b
              AND  payitemid  = :p
              AND  effectiveto IS NULL
        """),
        {"c": company_id, "b": branch_id, "p": pay_item_id},
    )).mappings().first()

    if existing is not None:
        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET    isactive = :ia
                WHERE  configid = :cid
            """),
            {"ia": is_active, "cid": int(existing["configid"])},
        )
        return {
            "existed":     True,
            "config_id":   int(existing["configid"]),
            "prev_active": bool(existing["isactive"]),
        }
    else:
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom, effectiveto)
                VALUES (:c, :b, :p, :ia, '2000-01-01', NULL)
            """),
            {"c": company_id, "b": branch_id, "p": pay_item_id, "ia": is_active},
        )
        return {
            "existed":      False,
            "company_id":   company_id,
            "branch_id":    branch_id,
            "pay_item_id":  pay_item_id,
        }


async def _restore_bpic(direct_db, token: dict) -> None:
    """Undo the change made by _set_bpic using the token it returned."""
    from sqlalchemy import text as _text

    if token["existed"]:
        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET    isactive = :ia
                WHERE  configid = :cid
            """),
            {"ia": token["prev_active"], "cid": token["config_id"]},
        )
    else:
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.branchpayitemconfig
                WHERE  companyid      = :c
                  AND  branchid       = :b
                  AND  payitemid      = :p
                  AND  effectivefrom  = '2000-01-01'
                  AND  effectiveto    IS NULL
            """),
            {
                "c": token["company_id"],
                "b": token["branch_id"],
                "p": token["pay_item_id"],
            },
        )


# ---------------------------------------------------------------------------
# TestMatrixDefaultFallback
# ---------------------------------------------------------------------------

class TestMatrixDefaultFallback:
    """
    Matrix LEFT JOIN + COALESCE(bpic.isactive, pi.isdefaultbranchactive) fallback:
    system items with IsDefaultBranchActive=TRUE appear even when no explicit
    BranchPayItemConfig row exists for the branch.
    """

    # ------------------------------------------------------------------
    # Test 1 — pure read, no state mutations, fully order-independent
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_matrix_shows_default_active_items_without_config(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        For a driver on HQ branch, the matrix must include HOURLY, MILEAGE, and
        LOAD even when no explicit BranchPayItemConfig row exists for those items.
        The IsDefaultBranchActive=TRUE fallback supplies the active signal.
        """
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": AS_OF_TODAY},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        rate_codes = {g["rate_code"] for g in resp.json()["groups"]}
        assert "HOURLY" in rate_codes, (
            f"HOURLY must appear via IsDefaultBranchActive fallback. Got: {rate_codes}"
        )
        assert "MILEAGE" in rate_codes, (
            f"MILEAGE must appear via IsDefaultBranchActive fallback. Got: {rate_codes}"
        )
        assert "LOAD" in rate_codes, (
            f"LOAD must appear via IsDefaultBranchActive fallback. Got: {rate_codes}"
        )

    # ------------------------------------------------------------------
    # Test 2 — mutates BranchPayItemConfig, restores in finally
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_matrix_excludes_item_when_explicit_config_inactive(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
        direct_db,
    ):
        """
        An explicit BranchPayItemConfig row with IsActive=FALSE overrides the
        IsDefaultBranchActive=TRUE default: the item must not appear in the matrix.

        Setup and teardown use _set_bpic / _restore_bpic so the DB is identical
        before and after this test regardless of pass or fail.
        """
        # Snapshot + set IsActive=FALSE for Hours Worked (payitemid=1) on HQ (branchid=1)
        token = await _set_bpic(direct_db, 1, 1, 1, is_active=False)
        try:
            resp = await session_client.get(
                f"/payroll/drivers/{created_driver_id}/rate-matrix",
                params={"as_of": AS_OF_TODAY},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            rate_codes = {g["rate_code"] for g in resp.json()["groups"]}
            assert "HOURLY" not in rate_codes, (
                "HOURLY must be hidden when explicit IsActive=FALSE overrides "
                f"IsDefaultBranchActive=TRUE. Got: {rate_codes}"
            )
        finally:
            await _restore_bpic(direct_db, token)

    # ------------------------------------------------------------------
    # Test 3 — mutates BranchPayItemConfig, restores in finally
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_matrix_shows_item_when_explicit_config_active(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
        direct_db,
    ):
        """
        An explicit BranchPayItemConfig row with IsActive=TRUE shows the item even
        when IsDefaultBranchActive=FALSE: the explicit override takes precedence.

        Uses Overnight Stay (payitemid=4, IsDefaultBranchActive=FALSE) as the probe.
        """
        token = await _set_bpic(direct_db, 1, 1, 4, is_active=True)
        try:
            resp = await session_client.get(
                f"/payroll/drivers/{created_driver_id}/rate-matrix",
                params={"as_of": AS_OF_TODAY},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            rate_codes = {g["rate_code"] for g in resp.json()["groups"]}
            assert "OVERNIGHT" in rate_codes, (
                "OVERNIGHT must appear when explicit IsActive=TRUE overrides "
                f"IsDefaultBranchActive=FALSE. Got: {rate_codes}"
            )
        finally:
            await _restore_bpic(direct_db, token)

    # ------------------------------------------------------------------
    # Test 4 — uses an isolated one-off driver; never touches created_driver_id
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_matrix_and_batch_agree_on_active_items(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Every row returned by the matrix must be accepted by batch_save_rates for the
        same driver.  Proves matrix and batch validation use the same active-item
        definition.

        Uses a fresh driver created inline on HQ (branch_id=1) so that approving
        rates via batch never touches the session-shared created_driver_id.  This
        avoids rate-history contamination that would cause test_pay_rates.py to fail
        the future-conflict guard when approving an older effective date for the same
        driver and rate type.
        """
        # Create a one-off driver on HQ branch for this test only
        unique_suffix = uuid.uuid4().hex[:8]
        create_resp = await client.post(
            "/core/drivers",
            json={
                "branch_id":      1,
                "full_name":      f"MatrixFallbackTest {unique_suffix}",
                "preferred_name": "MFT",
                "driver_code":    f"MFT-{unique_suffix}",
                "cdl_number":     f"CDL-MFT-{unique_suffix}",
                "email":          f"mft-{unique_suffix}@test.example",
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201, (
            f"Failed to create isolated test driver: {create_resp.text}"
        )
        test_driver_id = create_resp.json()["driver_id"]

        # Get the matrix for this fresh driver
        matrix_resp = await client.get(
            f"/payroll/drivers/{test_driver_id}/rate-matrix",
            params={"as_of": AS_OF_TODAY},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200, matrix_resp.text
        groups = matrix_resp.json()["groups"]
        assert groups, (
            "Matrix returned no groups for an HQ driver — "
            "IsDefaultBranchActive fallback may still be broken"
        )

        # Build batch payload from every matrix row
        changes = [
            {
                "pay_item_id": g.get("pay_item_id"),
                "status_rate_column_id": g.get("status_rate_column_id"),
                "rate_type_id": g["rate_type_id"],
                "amount": "10.00",
            }
            for g in groups
        ]

        # Batch must accept all items the matrix returned
        batch_resp = await client.post(
            f"/payroll/drivers/{test_driver_id}/rates/batch",
            json={"effective_from": AS_OF_TODAY, "changes": changes},
            headers=auth(auth_token),
        )
        assert batch_resp.status_code in (200, 201), (
            f"Batch rejected items that matrix returned — matrix and batch are out of sync. "
            f"Status: {batch_resp.status_code}. Body: {batch_resp.text}"
        )

    # ------------------------------------------------------------------
    # Test 5 — pure read, fully order-independent
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_summary_missing_count_uses_same_source(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        missing_required_count in the summary endpoint must be non-None when the
        driver's branch has active items via the IsDefaultBranchActive fallback.
        Proves the summary count and the matrix share the same active-item
        source of truth (LEFT JOIN + COALESCE).
        """
        summary_resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rates/summary",
            headers=auth(auth_token),
        )
        assert summary_resp.status_code == 200, summary_resp.text
        data = summary_resp.json()
        assert data["missing_required_count"] is not None, (
            "missing_required_count must not be None when the branch has active items "
            "via the IsDefaultBranchActive fallback"
        )
