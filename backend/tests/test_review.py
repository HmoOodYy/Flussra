"""
Integration tests for the review domain — GET/POST /review/items and
POST /review/items/{id}/decide.

Users tested
------------
admin        AllCompanyBranches scope, PAYROLL_ADMIN role (all permissions)
branch_user  SpecificBranch=HQ scope, PAYROLL_VIEWER role (no write permissions)

Test classes
------------
TestListItems       — list (empty, filtered by branch/status, branch scoping)
TestGetItem         — single item (200, 404, 403 cross-branch)
TestCreateItem      — create (201, validation, branch-scope denial, permission denial)
TestDecideItem      — decide (approve, reject, comment, status gate, permission denial)
TestReviewAudit     — audit-log rollback: create/decide roll back when _write_review_audit raises
"""
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from app.review import service as review_service


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Session-scoped fixture: one review item on HQ (created once, reused for reads)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def hq_review_item_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    hq_branch_id: int,
) -> int:
    """Create one Pending review item on the HQ branch at session start."""
    resp = await session_client.post(
        "/review/items",
        json={
            "branch_id":    hq_branch_id,
            "request_type": "DriverRateChange",
            "title":        "Seed: rate change request for TD1",
            "description":  "Rate increase from $0.40 to $0.45 per mile.",
            "priority":     "Normal",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"HQ review item seed failed: {resp.text}"
    return resp.json()["review_item_id"]


# ---------------------------------------------------------------------------
# TestListItems
# ---------------------------------------------------------------------------

class TestListItems:

    async def test_admin_sees_all_items(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_review_item_id: int,
    ):
        """Admin has AllCompanyBranches — must see at least the seeded HQ item."""
        resp = await client.get(
            "/review/items",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        ids = [i["review_item_id"] for i in resp.json()]
        assert hq_review_item_id in ids

    async def test_list_filter_by_branch(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        hq_review_item_id: int,
    ):
        """Filtering by hq_branch_id returns only HQ items."""
        resp = await client.get(
            "/review/items",
            params={"branch_id": hq_branch_id},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) > 0
        for item in items:
            assert item["branch_id"] == hq_branch_id

    async def test_list_filter_by_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_review_item_id: int,
    ):
        """Filtering by status=Pending must include the freshly-created item."""
        resp = await client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        items = resp.json()
        assert all(i["status"] == "Pending" for i in items)
        ids = [i["review_item_id"] for i in items]
        assert hq_review_item_id in ids

    async def test_branch_user_sees_hq_items(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
        hq_review_item_id: int,
    ):
        """branch_user has SpecificBranch=HQ scope — must see HQ items."""
        resp = await client.get(
            "/review/items",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 200
        items = resp.json()
        ids = [i["review_item_id"] for i in items]
        assert hq_review_item_id in ids
        # All returned items must be on HQ
        for item in items:
            assert item["branch_id"] == hq_branch_id

    async def test_branch_user_cannot_list_paytest_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        """Filtering to PAYTEST returns 403 for branch_user."""
        resp = await client.get(
            "/review/items",
            params={"branch_id": paytest_branch_id},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_unauthenticated_returns_401(self, client: httpx.AsyncClient):
        resp = await client.get("/review/items")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestGetItem
# ---------------------------------------------------------------------------

class TestGetItem:

    async def test_get_existing_item_returns_200(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_review_item_id: int,
    ):
        resp = await client.get(
            f"/review/items/{hq_review_item_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["review_item_id"] == hq_review_item_id
        assert body["status"] == "Pending"
        assert body["request_type"] == "DriverRateChange"
        assert isinstance(body["decisions"], list)

    async def test_get_nonexistent_item_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/review/items/99999999",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_branch_user_can_get_hq_item(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_review_item_id: int,
    ):
        """branch_user has HQ access — must be able to read an HQ item."""
        resp = await client.get(
            f"/review/items/{hq_review_item_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 200
        assert resp.json()["review_item_id"] == hq_review_item_id

    async def test_branch_user_cannot_get_paytest_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        """Admin creates a PAYTEST item; branch_user cannot read it (403)."""
        # Admin creates an item on PAYTEST
        create_resp = await session_client.post(
            "/review/items",
            json={
                "branch_id":    paytest_branch_id,
                "request_type": "Other",
                "title":        "PAYTEST item for cross-branch denial test",
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        paytest_item_id = create_resp.json()["review_item_id"]

        # branch_user tries to read it
        resp = await session_client.get(
            f"/review/items/{paytest_item_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestCreateItem
# ---------------------------------------------------------------------------

class TestCreateItem:

    async def test_admin_creates_item_returns_201(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    hq_branch_id,
                "request_type": "PayrollAdjustment",
                "title":        "Adjust overtime for driver ABC",
                "description":  "Missing 2 hours on 2040-03-15.",
                "priority":     "High",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "Pending"
        assert body["request_type"] == "PayrollAdjustment"
        assert body["branch_id"] == hq_branch_id
        assert body["priority"] == "High"
        # decisions list starts empty
        assert body["decisions"] == []

    async def test_create_with_entity_fields(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Entity fields (schema, name, id) are stored and returned."""
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":     hq_branch_id,
                "request_type":  "DriverRateChange",
                "title":         "Rate change with entity link",
                "entity_schema": "payroll",
                "entity_name":   "DriverRates",
                "entity_id":     "42",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["entity_schema"] == "payroll"
        assert body["entity_name"]   == "DriverRates"
        assert body["entity_id"]     == "42"

    async def test_create_invalid_request_type_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    hq_branch_id,
                "request_type": "NotARealType",
                "title":        "Should fail",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_blank_title_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    hq_branch_id,
                "request_type": "Other",
                "title":        "   ",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_viewer_cannot_create_item_on_hq(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """
        branch_user has HQ access but PAYROLL_VIEWER has no payroll.entry
        permission → 403 with 'permission' in detail.
        """
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    hq_branch_id,
                "request_type": "Other",
                "title":        "Viewer tries to create",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403
        assert "permission" in resp.json()["detail"].lower()

    async def test_branch_user_cannot_create_on_paytest(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        """Branch scope denial fires before permission check → 403."""
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    paytest_branch_id,
                "request_type": "Other",
                "title":        "Should be denied",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestDecideItem
# ---------------------------------------------------------------------------

class TestDecideItem:
    """
    Each test that mutates state creates its own fresh review item to avoid
    ordering dependencies with the session-scoped hq_review_item_id fixture.
    """

    async def _create_item(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        title: str = "Decide test item",
    ) -> int:
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    branch_id,
                "request_type": "Other",
                "title":        title,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        return resp.json()["review_item_id"]

    async def test_approve_updates_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        item_id = await self._create_item(client, auth_token, hq_branch_id, "Approve test")

        resp = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Approved", "decision_reason": "Looks good."},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "Approved"
        assert body["final_decision_by"] is not None
        assert body["final_decision_at_utc"] is not None
        assert body["final_decision_reason"] == "Looks good."
        # Decision history should contain the approval
        assert len(body["decisions"]) == 1
        assert body["decisions"][0]["decision"] == "Approved"

    async def test_reject_updates_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        item_id = await self._create_item(client, auth_token, hq_branch_id, "Reject test")

        resp = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Rejected", "decision_reason": "Insufficient justification."},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Rejected"

    async def test_edit_requested_keeps_item_decidable(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        EditRequested is a substantive decision: item status changes to
        EditRequested, which is still in _DECIDABLE_STATUSES, so a follow-up
        decision (e.g. Approved) is valid.
        """
        item_id = await self._create_item(
            client, auth_token, hq_branch_id, "EditRequested then Approved"
        )

        # First decision: EditRequested
        r1 = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "EditRequested", "decision_reason": "Please add more detail."},
            headers=auth(auth_token),
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "EditRequested"

        # Second decision: Approved (item is EditRequested → still decidable)
        r2 = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Approved", "decision_reason": "Detail added, approved."},
            headers=auth(auth_token),
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "Approved"
        assert len(r2.json()["decisions"]) == 2

    async def test_comment_does_not_change_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Comment decision appends to history but leaves item Status as Pending."""
        item_id = await self._create_item(client, auth_token, hq_branch_id, "Comment test")

        resp = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Comment", "decision_reason": "Noted for the record."},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        # Status must still be Pending
        assert body["status"] == "Pending"
        # final_decision fields must NOT be set (Comment is not a final decision)
        assert body["final_decision_at_utc"] is None
        assert body["final_decision_by"] is None
        # But the decision is recorded in history
        assert len(body["decisions"]) == 1
        assert body["decisions"][0]["decision"] == "Comment"

    async def test_cannot_decide_on_approved_item(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """A terminal-status item (Approved) cannot receive further decisions → 422."""
        item_id = await self._create_item(
            client, auth_token, hq_branch_id, "Already approved item"
        )
        # Approve it
        r1 = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert r1.status_code == 200

        # Try to decide again
        r2 = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Rejected"},
            headers=auth(auth_token),
        )
        assert r2.status_code == 422

    async def test_decide_nonexistent_item_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/review/items/99999999/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_viewer_cannot_decide(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """
        branch_user has HQ access but PAYROLL_VIEWER has no review.decide
        permission → 403 with 'permission' in detail.
        """
        # Admin creates an item branch_user can see
        item_id = await self._create_item(
            session_client, auth_token, hq_branch_id, "Viewer permission denial"
        )

        resp = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Approved"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403
        assert "permission" in resp.json()["detail"].lower()

    async def test_decide_invalid_decision_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        item_id = await self._create_item(
            client, auth_token, hq_branch_id, "Invalid decision test"
        )
        resp = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "MaybeLater"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestReviewAudit — rollback tests for audit log writes
# ---------------------------------------------------------------------------

class TestReviewAudit:
    """
    Verify that audit.auditlog writes are inside the same transaction as the
    primary writes, so a failure rolls back everything atomically.

    Pattern mirrors TestRateSafety.test_rollback_when_audit_fails_* in test_rates.py:
      1. Monkeypatch _write_review_audit to raise RuntimeError.
      2. Call the endpoint — ASGITransport re-raises the unhandled exception.
      3. After the exception, verify the primary write was NOT committed.

    This also proves that _write_review_audit IS being called (if it weren't,
    the monkeypatched raise would never fire and no RuntimeError would propagate).
    """

    async def test_rollback_when_audit_fails_on_create(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        If _write_review_audit raises during create_review_item(), the item
        INSERT must roll back — no row in the DB after the exception.
        """
        headers = auth(auth_token)

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on review create — rollback expected")

        with patch.object(review_service, "_write_review_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on review create"):
                await client.post(
                    "/review/items",
                    json={
                        "branch_id":    hq_branch_id,
                        "request_type": "Other",
                        "title":        "Audit rollback test item — must not exist",
                    },
                    headers=headers,
                )

        # The item must not have been committed.
        resp = await client.get(
            "/review/items",
            params={"branch_id": hq_branch_id, "status": "Pending"},
            headers=headers,
        )
        assert resp.status_code == 200
        titles = [i["title"] for i in resp.json()]
        assert "Audit rollback test item — must not exist" not in titles

    async def test_rollback_when_audit_fails_on_decide(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        If _write_review_audit raises during decide_review_item(), the decision
        INSERT and the item status UPDATE must both roll back.

        After the exception:
          - The item must still be in Pending status.
          - The ManagerReviewDecisions table must have no row for this item.
          (The latter is verified indirectly: the item's decisions list is empty
           when fetched, since both writes were in the same transaction.)
        """
        headers = auth(auth_token)

        # Create the item without the monkeypatch (audit must work here).
        create_resp = await client.post(
            "/review/items",
            json={
                "branch_id":    hq_branch_id,
                "request_type": "Other",
                "title":        "Decide audit rollback test",
            },
            headers=headers,
        )
        assert create_resp.status_code == 201
        item_id = create_resp.json()["review_item_id"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on review decide — rollback expected")

        with patch.object(review_service, "_write_review_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on review decide"):
                await client.post(
                    f"/review/items/{item_id}/decide",
                    json={"decision": "Approved", "decision_reason": "Should roll back."},
                    headers=headers,
                )

        # Fetch the item — status must still be Pending and decisions must be empty.
        fetch = await client.get(f"/review/items/{item_id}", headers=headers)
        assert fetch.status_code == 200
        body = fetch.json()
        assert body["status"] == "Pending", (
            f"Item status should still be Pending after rollback, got {body['status']!r}"
        )
        assert body["decisions"] == [], (
            f"Decisions list should be empty after rollback, got {body['decisions']}"
        )


# ---------------------------------------------------------------------------
# TestSelfApprovalPolicy — AllowSelfApproval company setting
# ---------------------------------------------------------------------------

class TestSelfApprovalPolicy:
    """
    Tests for the AllowSelfApproval company policy enforced in decide_review_item.

    Design notes
    ------------
    - The DEMO company starts with AllowSelfApproval = TRUE (migration default).
    - Tests that set it to FALSE restore it to TRUE before returning so they
      don't interfere with other tests in the session.
    - A second reviewer user (PAYROLL_ADMIN role, AllCompanyBranches scope) is
      created once per class via a session-scoped fixture so the "different user
      can decide" tests have a valid second actor.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    async def _set_self_approval(
        client: httpx.AsyncClient,
        token: str,
        value: bool,
    ) -> None:
        """PATCH the company profile to set allow_self_approval."""
        resp = await client.patch(
            "/settings/company",
            json={
                "company_name":       "Demo Logistics",
                "allow_self_approval": value,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, (
            f"Failed to set allow_self_approval={value}: {resp.text}"
        )

    @staticmethod
    async def _create_item(
        client: httpx.AsyncClient,
        token: str,
        branch_id: int,
        title: str,
    ) -> int:
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    branch_id,
                "request_type": "Other",
                "title":        title,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, f"Create item failed: {resp.text}"
        return resp.json()["review_item_id"]

    # ------------------------------------------------------------------
    # Test: default policy (AllowSelfApproval = TRUE) still works
    # ------------------------------------------------------------------

    async def test_self_approval_allowed_when_policy_is_true(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        Default: AllowSelfApproval = TRUE.
        The user who creates the item may also approve it.
        """
        # Ensure policy is TRUE (should already be the default).
        await self._set_self_approval(client, auth_token, True)

        item_id = await self._create_item(
            client, auth_token, hq_branch_id, "Self-approval allowed test"
        )
        resp = await client.post(
            f"/review/items/{item_id}/decide",
            json={"decision": "Approved", "decision_reason": "Self-approval allowed."},
            headers=self._auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Approved"

    # ------------------------------------------------------------------
    # Test: policy = FALSE blocks the submitter from approving
    # ------------------------------------------------------------------

    async def test_self_approval_blocked_when_policy_is_false(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        AllowSelfApproval = FALSE.
        The submitter cannot approve their own item → HTTP 422.
        """
        await self._set_self_approval(client, auth_token, False)
        try:
            item_id = await self._create_item(
                client, auth_token, hq_branch_id, "Self-approval blocked test"
            )
            resp = await client.post(
                f"/review/items/{item_id}/decide",
                json={"decision": "Approved", "decision_reason": "Should be blocked."},
                headers=self._auth(auth_token),
            )
            assert resp.status_code == 422
            assert "self-approval" in resp.json()["detail"].lower()
        finally:
            await self._set_self_approval(client, auth_token, True)

    async def test_reject_blocked_when_policy_is_false(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        AllowSelfApproval = FALSE blocks Rejected decisions by the submitter too.
        """
        await self._set_self_approval(client, auth_token, False)
        try:
            item_id = await self._create_item(
                client, auth_token, hq_branch_id, "Self-reject blocked test"
            )
            resp = await client.post(
                f"/review/items/{item_id}/decide",
                json={"decision": "Rejected"},
                headers=self._auth(auth_token),
            )
            assert resp.status_code == 422
            assert "self-approval" in resp.json()["detail"].lower()
        finally:
            await self._set_self_approval(client, auth_token, True)

    async def test_comment_allowed_even_when_policy_is_false(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        AllowSelfApproval = FALSE does NOT block Comment decisions.
        Comments are discussion notes, not substantive sign-off actions.
        """
        await self._set_self_approval(client, auth_token, False)
        try:
            item_id = await self._create_item(
                client, auth_token, hq_branch_id, "Self-comment still allowed test"
            )
            resp = await client.post(
                f"/review/items/{item_id}/decide",
                json={"decision": "Comment", "decision_reason": "Just a note."},
                headers=self._auth(auth_token),
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "Pending"  # Comment does not change status
        finally:
            await self._set_self_approval(client, auth_token, True)

    async def test_different_user_can_decide_when_policy_is_false(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        AllowSelfApproval = FALSE: a different user with review.decide CAN approve.

        Creates a second admin user inline, obtains their token, and uses it to
        approve an item originally submitted by the primary admin user.
        """
        await self._set_self_approval(client, auth_token, False)
        try:
            # Create the item as the primary admin.
            item_id = await self._create_item(
                client, auth_token, hq_branch_id, "Different reviewer test"
            )

            # Create a second PAYROLL_ADMIN user via the admin API.
            create_resp = await session_client.post(
                "/admin/users",
                json={
                    "username":    "reviewer2",
                    "display_name": "Reviewer Two",
                    "password":    "TestPass123!",
                },
                headers=self._auth(auth_token),
            )
            assert create_resp.status_code == 201, (
                f"Failed to create reviewer2: {create_resp.text}"
            )
            reviewer2_id = create_resp.json()["user_id"]

            # Assign PAYROLL_ADMIN role (AllCompanyBranches) to reviewer2.
            roles_resp = await session_client.get(
                "/admin/roles",
                headers=self._auth(auth_token),
            )
            assert roles_resp.status_code == 200
            admin_role = next(
                r for r in roles_resp.json() if r["role_code"] == "PAYROLL_ADMIN"
            )
            assign_resp = await session_client.post(
                f"/admin/users/{reviewer2_id}/roles",
                json={
                    "role_id":    admin_role["role_id"],
                    "scope_type": "AllCompanyBranches",
                },
                headers=self._auth(auth_token),
            )
            assert assign_resp.status_code == 201, (
                f"Failed to assign role to reviewer2: {assign_resp.text}"
            )

            # Log in as reviewer2.
            login_resp = await session_client.post("/auth/login", json={
                "username":     "reviewer2",
                "password":     "TestPass123!",
                "company_code": "DEMO",
            })
            assert login_resp.status_code == 200
            reviewer2_token = login_resp.json()["access_token"]

            # reviewer2 approves the item — this MUST succeed (different user).
            decide_resp = await client.post(
                f"/review/items/{item_id}/decide",
                json={"decision": "Approved", "decision_reason": "Approved by second reviewer."},
                headers=self._auth(reviewer2_token),
            )
            assert decide_resp.status_code == 200, (
                f"Different user should be able to approve: {decide_resp.text}"
            )
            assert decide_resp.json()["status"] == "Approved"
        finally:
            await self._set_self_approval(client, auth_token, True)

    # ------------------------------------------------------------------
    # Test: verify company profile reflects the policy correctly
    # ------------------------------------------------------------------

    async def test_company_profile_exposes_allow_self_approval(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """GET /settings/company returns allow_self_approval in the response."""
        # Ensure it is TRUE first.
        await self._set_self_approval(client, auth_token, True)
        resp = await client.get("/settings/company", headers=self._auth(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        assert "allow_self_approval" in body
        assert body["allow_self_approval"] is True

        # Flip to FALSE and verify.
        try:
            await self._set_self_approval(client, auth_token, False)
            resp2 = await client.get("/settings/company", headers=self._auth(auth_token))
            assert resp2.status_code == 200
            assert resp2.json()["allow_self_approval"] is False
        finally:
            await self._set_self_approval(client, auth_token, True)
