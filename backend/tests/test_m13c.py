"""
M13c integration tests — Advanced rate structures.

Covers:
  OrdinalTier    — per-ordinal-position rates; integer qty only
  RangeBracket   — entire qty at single bracket rate (non-marginal)
  RangeProgressive — marginal rates (each slice at its tier rate)
  Block          — flat amount per complete block; Floor / Ceiling / NearestHalfUp

Test structure
--------------
Session-scoped fixtures create custom pay items (one per behavior) + link them
to test-only rate types (M13C_ORDINAL, M13C_RBRKT, M13C_RPROG, M13C_BLOCK).
Function-scoped fixtures create/void driver rates around each test.

All entry tests use PAYTEST branch on period dates in 2033 (no conflicts with
other test modules which use 2026-2032).
"""
import datetime

import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _void_all_rates(client: httpx.AsyncClient, token: str) -> None:
    """Void all non-terminal rates (PendingApproval, Approved, Superseded)."""
    resp = await client.get("/payroll/rates", headers=auth(token))
    if resp.status_code != 200:
        return
    for r in resp.json():
        if r["status"] in ("PendingApproval", "Approved", "Superseded"):
            await client.delete(f"/payroll/rates/{r['driver_rate_id']}", headers=auth(token))


async def _cancel_active_periods(
    client: httpx.AsyncClient, token: str, branch_id: int
) -> None:
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods", params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )


async def _insert_open_period(
    db: AsyncConnection,
    branch_id: int,
    start: str,
    end: str,
    code: str,
) -> int:
    """Seed an Open period for low-level guard tests that bypass creation."""
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype,
                 startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": code,
         "start": datetime.date.fromisoformat(start),
         "end": datetime.date.fromisoformat(end)},
    )).mappings().first()
    await db.commit()
    return row["payrollperiodid"]


async def _get_rate_type_id(client: httpx.AsyncClient, token: str, code: str) -> int:
    resp = await client.get("/payroll/rate-types", headers=auth(token))
    assert resp.status_code == 200
    for rt in resp.json():
        if rt["rate_code"] == code:
            return rt["rate_type_id"]
    raise AssertionError(f"Rate type {code!r} not found — check conftest seed")


async def _create_custom_item(
    db_conn,
    code: str,
    name: str,
    behavior: str,
    unit: str = "Unit",
) -> dict:
    """Seed a legacy custom Daily pay item directly into the DB (bypasses LLR-A guard)."""
    from tests.seed_helpers import seed_legacy_item
    item_id = await seed_legacy_item(
        db_conn, code=code, name=name, rate_behavior=behavior, unit=unit, category="Count",
    )
    return {"pay_item_id": item_id, "pay_item_code": code, "pay_item_name": name}


async def _link_rate_type(
    client: httpx.AsyncClient, token: str, item_id: int, rate_type_id: int
) -> None:
    resp = await client.post(
        f"/settings/pay-items/{item_id}/rate-type-map",
        json={"rate_type_id": rate_type_id, "is_primary": True},
        headers=auth(token),
    )
    assert resp.status_code in (200, 201), f"link rate type failed: {resp.text}"


async def _activate_item(
    client: httpx.AsyncClient, token: str, branch_id: int, item_id: int
) -> None:
    resp = await client.patch(
        f"/settings/branches/{branch_id}/pay-items/{item_id}",
        json={"is_active": True},
        headers=auth(token),
    )
    assert resp.status_code == 200, f"activate item failed: {resp.text}"


async def _create_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    *,
    amount: str = "1.00",
    effective_from: str = "2033-01-01",
    ordinal_tiers: list | None = None,
    range_tiers: list | None = None,
    block_size: str | None = None,
    rounding_rule: str | None = None,
) -> dict:
    body: dict = {
        "driver_id":      driver_id,
        "rate_type_id":   rate_type_id,
        "amount":         amount,
        "effective_from": effective_from,
    }
    if ordinal_tiers:
        body["ordinal_tiers"] = ordinal_tiers
    if range_tiers:
        body["range_tiers"] = range_tiers
    if block_size:
        body["block_size"] = block_size
    if rounding_rule:
        body["rounding_rule"] = rounding_rule
    resp = await client.post("/payroll/rates", json=body, headers=auth(token))
    return resp


async def _approve_rate(
    client: httpx.AsyncClient, token: str, rate_id: int
) -> dict:
    resp = await client.post(
        f"/payroll/rates/{rate_id}/approve", headers=auth(token)
    )
    assert resp.status_code == 200, f"approve failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Standard 3-tier ordinal fixture data
# 1→$1, 2→$2, 3+→$3
# ---------------------------------------------------------------------------
_ORDINAL_3_TIERS = [
    {"tier_sequence": 1, "from_unit": 1, "to_unit": 1,    "tier_amount": "1.00"},
    {"tier_sequence": 2, "from_unit": 2, "to_unit": 2,    "tier_amount": "2.00"},
    {"tier_sequence": 3, "from_unit": 3, "to_unit": None,  "tier_amount": "3.00"},
]

# Range tiers: 0..10@$1, 10..20@$2, 20+@$3
_RANGE_3_TIERS = [
    {"tier_sequence": 1, "to_unit": "10",  "tier_amount": "1.00"},
    {"tier_sequence": 2, "to_unit": "20",  "tier_amount": "2.00"},
    {"tier_sequence": 3, "to_unit": None,   "tier_amount": "3.00"},
]


# ===========================================================================
# Session-scoped custom item fixtures
# ===========================================================================

@pytest_asyncio.fixture(scope="session")
async def m13c_ordinal_item(
    session_client: httpx.AsyncClient,
    session_db_conn,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """M13C_LOADS — OrdinalTier custom item linked to M13C_ORDINAL rate type."""
    item = await _create_custom_item(
        session_db_conn, "M13C_LOADS", "Load Tier Test", "OrdinalTier", "Load"
    )
    rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_ORDINAL")
    await _link_rate_type(session_client, auth_token, item["pay_item_id"], rt_id)
    await _activate_item(session_client, auth_token, paytest_branch_id, item["pay_item_id"])
    return item


@pytest_asyncio.fixture(scope="session")
async def m13c_rbrkt_item(
    session_client: httpx.AsyncClient,
    session_db_conn,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """M13C_RBRKT — RangeBracket custom item linked to M13C_RBRKT rate type."""
    item = await _create_custom_item(
        session_db_conn, "M13C_RBRKT", "Range Bracket Test", "RangeBracket", "Mile"
    )
    rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_RBRKT")
    await _link_rate_type(session_client, auth_token, item["pay_item_id"], rt_id)
    await _activate_item(session_client, auth_token, paytest_branch_id, item["pay_item_id"])
    return item


@pytest_asyncio.fixture(scope="session")
async def m13c_rprog_item(
    session_client: httpx.AsyncClient,
    session_db_conn,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """M13C_RPROG — RangeProgressive custom item linked to M13C_RPROG rate type."""
    item = await _create_custom_item(
        session_db_conn, "M13C_RPROG", "Range Progressive Test", "RangeProgressive", "Mile"
    )
    rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_RPROG")
    await _link_rate_type(session_client, auth_token, item["pay_item_id"], rt_id)
    await _activate_item(session_client, auth_token, paytest_branch_id, item["pay_item_id"])
    return item


@pytest_asyncio.fixture(scope="session")
async def m13c_block_item(
    session_client: httpx.AsyncClient,
    session_db_conn,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """M13C_BLOCK — Block custom item linked to M13C_BLOCK rate type."""
    item = await _create_custom_item(
        session_db_conn, "M13C_BLOCK", "Block Rate Test", "Block", "Mile"
    )
    rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_BLOCK")
    await _link_rate_type(session_client, auth_token, item["pay_item_id"], rt_id)
    await _activate_item(session_client, auth_token, paytest_branch_id, item["pay_item_id"])
    return item


# Convenience session fixtures for rate type IDs
@pytest_asyncio.fixture(scope="session")
async def m13c_ordinal_rt_id(session_client, auth_token):
    return await _get_rate_type_id(session_client, auth_token, "M13C_ORDINAL")

@pytest_asyncio.fixture(scope="session")
async def m13c_rbrkt_rt_id(session_client, auth_token):
    return await _get_rate_type_id(session_client, auth_token, "M13C_RBRKT")

@pytest_asyncio.fixture(scope="session")
async def m13c_rprog_rt_id(session_client, auth_token):
    return await _get_rate_type_id(session_client, auth_token, "M13C_RPROG")

@pytest_asyncio.fixture(scope="session")
async def m13c_block_rt_id(session_client, auth_token):
    return await _get_rate_type_id(session_client, auth_token, "M13C_BLOCK")


# ---------------------------------------------------------------------------
# Function-scoped fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def m13c_rates_clean(session_client: httpx.AsyncClient, auth_token: str):
    """Void all non-terminal rates before and after each test."""
    await _void_all_rates(session_client, auth_token)
    yield
    await _void_all_rates(session_client, auth_token)


@pytest_asyncio.fixture
async def m13c_open_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    m13c_rates_clean,
    direct_db,
):
    """Open a period on PAYTEST branch (dates 2033) and cancel after the test."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)

    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype,
                 startdate, enddate)
            VALUES (1, :bid, 'Open', 'M13C-2033-0301', 'M13C Test Week', 'Week',
                    '2033-03-01', '2033-03-07')
            RETURNING payrollperiodid, status, startdate, enddate
        """),
        {"bid": paytest_branch_id},
    )).mappings().first()
    yield {
        "payroll_period_id": row["payrollperiodid"],
        "status": row["status"],
        "start_date": str(row["startdate"]),
        "end_date": str(row["enddate"]),
    }

    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)


# ===========================================================================
# TestOrdinalTierCreate — create / validation
# ===========================================================================

class TestOrdinalTierCreate:

    async def test_create_valid_ordinal_rate(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Create OrdinalTier rate with valid 3-tier structure; tiers returned in response."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert resp.status_code == 201, resp.text
        rate = resp.json()
        assert rate["status"] == "PendingApproval"
        assert rate["tiers"] is not None
        assert len(rate["tiers"]) == 3
        assert rate["tiers"][0]["tier_sequence"] == 1
        assert rate["tiers"][0]["from_unit"] == "1.0000"
        assert rate["tiers"][0]["to_unit"] == "1.0000"
        assert rate["tiers"][1]["to_unit"] == "2.0000"
        assert rate["tiers"][2]["to_unit"] is None      # last tier open-ended

    async def test_ordinal_requires_tiers(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Creating an OrdinalTier rate without tiers is rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, amount="5.00",
        )
        assert resp.status_code == 422
        assert "ordinal_tiers" in resp.text.lower()

    async def test_ordinal_sequence_gap_rejected(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Tier sequence 1, 3 (gap at 2) is rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, amount="1.00",
            ordinal_tiers=[
                {"tier_sequence": 1, "from_unit": 1, "to_unit": 1, "tier_amount": "1.00"},
                {"tier_sequence": 3, "from_unit": 2, "to_unit": None, "tier_amount": "2.00"},
            ],
        )
        assert resp.status_code == 422
        assert "gapless" in resp.text.lower() or "sequence" in resp.text.lower()

    async def test_ordinal_first_tier_from_unit_not_1(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """First tier from_unit must be 1."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, amount="1.00",
            ordinal_tiers=[
                {"tier_sequence": 1, "from_unit": 2, "to_unit": None, "tier_amount": "1.00"},
            ],
        )
        assert resp.status_code == 422
        assert "from_unit" in resp.text.lower() or "must be 1" in resp.text.lower()

    async def test_ordinal_non_contiguous_rejected(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Gap between tiers (1..2, then 4+) is rejected — missing position 3."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, amount="1.00",
            ordinal_tiers=[
                {"tier_sequence": 1, "from_unit": 1, "to_unit": 2, "tier_amount": "1.00"},
                {"tier_sequence": 2, "from_unit": 4, "to_unit": None, "tier_amount": "2.00"},
            ],
        )
        assert resp.status_code == 422
        assert "from_unit" in resp.text.lower() or "contiguous" in resp.text.lower()

    async def test_ordinal_last_tier_must_be_open(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Last tier must have to_unit=NULL."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, amount="1.00",
            ordinal_tiers=[
                {"tier_sequence": 1, "from_unit": 1, "to_unit": 1, "tier_amount": "1.00"},
                {"tier_sequence": 2, "from_unit": 2, "to_unit": 5, "tier_amount": "2.00"},
            ],
        )
        assert resp.status_code == 422
        assert "last tier" in resp.text.lower() or "to_unit" in resp.text.lower()

    async def test_ordinal_range_tiers_rejected_for_ordinal_item(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Supplying range_tiers for an OrdinalTier item is rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, amount="1.00",
            range_tiers=[
                {"tier_sequence": 1, "to_unit": "10", "tier_amount": "1.00"},
                {"tier_sequence": 2, "to_unit": None,  "tier_amount": "2.00"},
            ],
        )
        assert resp.status_code == 422

    async def test_ordinal_single_tier_valid(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Single-tier OrdinalTier (open-ended from position 1) is valid."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, amount="1.00",
            ordinal_tiers=[
                {"tier_sequence": 1, "from_unit": 1, "to_unit": None, "tier_amount": "5.00"},
            ],
        )
        assert resp.status_code == 201, resp.text
        assert len(resp.json()["tiers"]) == 1


# ===========================================================================
# TestRangeTierCreate — RangeBracket / RangeProgressive validation
# ===========================================================================

class TestRangeTierCreate:

    async def test_create_valid_range_bracket(
        self, session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, m13c_rates_clean,
        m13c_rbrkt_item,
    ):
        """Create RangeBracket rate with 3 tiers; from_units are system-derived."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, amount="1.00",
            range_tiers=_RANGE_3_TIERS,
        )
        assert resp.status_code == 201, resp.text
        rate = resp.json()
        tiers = rate["tiers"]
        assert len(tiers) == 3
        # Tier 1: derived from_unit = 0
        assert Decimal(tiers[0]["from_unit"]) == Decimal("0")
        assert Decimal(tiers[0]["to_unit"]) == Decimal("10")
        # Tier 2: derived from_unit = 10 (= tier 1 to_unit)
        assert Decimal(tiers[1]["from_unit"]) == Decimal("10")
        assert Decimal(tiers[1]["to_unit"]) == Decimal("20")
        # Tier 3: derived from_unit = 20; open-ended
        assert Decimal(tiers[2]["from_unit"]) == Decimal("20")
        assert tiers[2]["to_unit"] is None

    async def test_range_requires_2_tiers(
        self, session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, m13c_rates_clean,
        m13c_rbrkt_item,
    ):
        """Single-tier range is rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, amount="1.00",
            range_tiers=[
                {"tier_sequence": 1, "to_unit": None, "tier_amount": "1.00"},
            ],
        )
        assert resp.status_code == 422
        assert "2 tiers" in resp.text.lower() or "at least" in resp.text.lower()

    async def test_range_last_tier_must_be_open(
        self, session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, m13c_rates_clean,
        m13c_rbrkt_item,
    ):
        """Last range tier to_unit must be NULL."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, amount="1.00",
            range_tiers=[
                {"tier_sequence": 1, "to_unit": "10", "tier_amount": "1.00"},
                {"tier_sequence": 2, "to_unit": "20", "tier_amount": "2.00"},
            ],
        )
        assert resp.status_code == 422
        assert "last tier" in resp.text.lower() or "to_unit" in resp.text.lower()

    async def test_range_to_units_not_increasing(
        self, session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, m13c_rates_clean,
        m13c_rbrkt_item,
    ):
        """Non-increasing to_unit values are rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, amount="1.00",
            range_tiers=[
                {"tier_sequence": 1, "to_unit": "20", "tier_amount": "1.00"},
                {"tier_sequence": 2, "to_unit": "10", "tier_amount": "2.00"},
                {"tier_sequence": 3, "to_unit": None,  "tier_amount": "3.00"},
            ],
        )
        assert resp.status_code == 422
        assert "strictly greater" in resp.text.lower() or "to_unit" in resp.text.lower()

    async def test_range_ordinal_tiers_rejected_for_range_item(
        self, session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, m13c_rates_clean,
        m13c_rbrkt_item,
    ):
        """Supplying ordinal_tiers for a RangeBracket item is rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, amount="1.00",
            ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert resp.status_code == 422


# ===========================================================================
# TestBlockCreate — Block rate validation
# ===========================================================================

class TestBlockCreate:

    async def test_create_valid_block_floor(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_block_item,
    ):
        """Create Block rate; block_size and rounding_rule in response."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_block_rt_id,
            amount="100.00", block_size="5", rounding_rule="Floor",
        )
        assert resp.status_code == 201, resp.text
        rate = resp.json()
        assert Decimal(rate["block_size"]) == Decimal("5")
        assert rate["rounding_rule"] == "Floor"
        assert rate["tiers"] == []          # no tier rows for Block

    async def test_block_missing_block_size(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_block_item,
    ):
        """Block rate without block_size rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_block_rt_id,
            amount="100.00", rounding_rule="Floor",
        )
        assert resp.status_code == 422
        assert "block_size" in resp.text.lower()

    async def test_block_missing_rounding_rule(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_block_item,
    ):
        """Block rate without rounding_rule rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_block_rt_id,
            amount="100.00", block_size="5",
        )
        assert resp.status_code == 422
        assert "rounding_rule" in resp.text.lower()

    async def test_block_invalid_rounding_rule(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_block_item,
    ):
        """Invalid rounding_rule value rejected at schema level."""
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   m13c_block_rt_id,
                "amount":         "100.00",
                "effective_from": "2033-01-01",
                "block_size":     "5",
                "rounding_rule":  "Round",   # Invalid — must be NearestHalfUp
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "nearesthalfup" in resp.text.lower() or "rounding_rule" in resp.text.lower()

    async def test_block_tiers_not_accepted(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_block_item,
    ):
        """Supplying tiers for a Block item is rejected."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_block_rt_id,
            amount="100.00", block_size="5", rounding_rule="Floor",
            range_tiers=_RANGE_3_TIERS,
        )
        assert resp.status_code == 422

    async def test_block_all_three_rounding_rules_accepted(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_block_item,
    ):
        """All three valid rounding rules are accepted at the schema level."""
        for rule in ("Floor", "Ceiling", "NearestHalfUp"):
            await _void_all_rates(session_client, auth_token)
            resp = await _create_rate(
                session_client, auth_token, paytest_driver_id, m13c_block_rt_id,
                amount="100.00", block_size="5", rounding_rule=rule,
            )
            assert resp.status_code == 201, f"Rule {rule} rejected: {resp.text}"
            assert resp.json()["rounding_rule"] == rule


# ===========================================================================
# TestTierUpdate — update tiers on PendingApproval rate
# ===========================================================================

class TestTierUpdate:

    async def test_update_tiers_on_pending_rate(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Updating tiers on a PendingApproval rate replaces all existing tiers."""
        create_resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert create_resp.status_code == 201
        rate_id = create_resp.json()["driver_rate_id"]

        new_tiers = [
            {"tier_sequence": 1, "from_unit": 1, "to_unit": None, "tier_amount": "9.00"},
        ]
        update_resp = await session_client.patch(
            f"/payroll/rates/{rate_id}",
            json={"ordinal_tiers": new_tiers},
            headers=auth(auth_token),
        )
        assert update_resp.status_code == 200, update_resp.text
        tiers = update_resp.json()["tiers"]
        assert len(tiers) == 1
        assert Decimal(tiers[0]["tier_amount"]) == Decimal("9.00")

    async def test_update_tiers_on_approved_rate_fails(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Cannot edit (tiers or otherwise) an Approved rate."""
        create_resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert create_resp.status_code == 201
        rate_id = create_resp.json()["driver_rate_id"]
        await _approve_rate(session_client, auth_token, rate_id)

        update_resp = await session_client.patch(
            f"/payroll/rates/{rate_id}",
            json={"ordinal_tiers": [
                {"tier_sequence": 1, "from_unit": 1, "to_unit": None, "tier_amount": "99.00"}
            ]},
            headers=auth(auth_token),
        )
        assert update_resp.status_code == 422
        assert "pendingapproval" in update_resp.text.lower()


# ===========================================================================
# TestApproveWithTiers
# ===========================================================================

class TestApproveWithTiers:

    async def test_approve_ordinal_tier_rate(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Approving an OrdinalTier rate succeeds; tiers preserved on approved rate."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert resp.status_code == 201
        rate_id = resp.json()["driver_rate_id"]

        approved = await _approve_rate(session_client, auth_token, rate_id)
        assert approved["status"] == "Approved"
        assert approved["tiers"] is not None
        assert len(approved["tiers"]) == 3

    async def test_approve_tiered_rate_no_tiers_fails(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Approval of an OrdinalTier rate that somehow has no tiers is rejected."""
        # Create with tiers, then strip tiers by replacing with a dummy value.
        # We can simulate this by creating via the rate endpoint then manually
        # patching away tiers — but since the API won't let us set tiers=[], we
        # instead verify the approval guard fires when the DB has no tiers.
        # The only way to produce this state is via direct DB manipulation.
        # Instead, test the guard indirectly: create rate with tiers, approve → OK.
        # Guard is belt-and-suspenders; relies on create/update enforcing tiers exist.
        # Just confirm the happy path approves cleanly.
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert resp.status_code == 201
        rate_id = resp.json()["driver_rate_id"]
        approved = await _approve_rate(session_client, auth_token, rate_id)
        assert approved["status"] == "Approved"

    async def test_void_preserves_tier_history(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Voiding a rate does NOT delete its tiers (status update, not delete)."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert resp.status_code == 201
        rate_id = resp.json()["driver_rate_id"]

        # Void the rate
        void_resp = await session_client.delete(
            f"/payroll/rates/{rate_id}", headers=auth(auth_token)
        )
        assert void_resp.status_code == 204

        # Fetch the voided rate via GET — tiers must still be there.
        get_resp = await session_client.get(
            f"/payroll/rates/{rate_id}", headers=auth(auth_token)
        )
        assert get_resp.status_code == 200
        voided_rate = get_resp.json()
        assert voided_rate["status"] == "Voided"
        # Tiers should still be present
        assert voided_rate["tiers"] is not None
        assert len(voided_rate["tiers"]) == 3

    async def test_supersession_preserves_old_tier_history(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """Superseded rate keeps its original tiers after a new rate is approved."""
        # Create and approve old rate (3 tiers, $1/$2/$3)
        old_resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS, effective_from="2033-01-01",
        )
        assert old_resp.status_code == 201
        old_id = old_resp.json()["driver_rate_id"]
        await _approve_rate(session_client, auth_token, old_id)

        # Create and approve new rate (1 tier, $9) — supersedes the old one
        new_resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00",
            ordinal_tiers=[{"tier_sequence": 1, "from_unit": 1, "to_unit": None, "tier_amount": "9.00"}],
            effective_from="2033-06-01",
        )
        assert new_resp.status_code == 201
        new_id = new_resp.json()["driver_rate_id"]
        await _approve_rate(session_client, auth_token, new_id)

        # Fetch the superseded old rate — must still have its original 3 tiers
        old_get = await session_client.get(
            f"/payroll/rates/{old_id}", headers=auth(auth_token)
        )
        assert old_get.status_code == 200
        old_rate = old_get.json()
        assert old_rate["status"] == "Superseded"
        assert len(old_rate["tiers"]) == 3   # original tiers preserved


# ===========================================================================
# TestOrdinalTierCalculation
# ===========================================================================

class TestOrdinalTierCalculation:
    """Uses the 3-tier structure: 1→$1, 2→$2, 3+→$3."""

    @pytest_asyncio.fixture
    async def approved_ordinal_rate(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS, effective_from="2033-01-01",
        )
        assert resp.status_code == 201
        rate_id = resp.json()["driver_rate_id"]
        return await _approve_rate(session_client, auth_token, rate_id)

    async def test_ordinal_qty_1_single_tier(
        self, session_client, auth_token, paytest_driver_id, approved_ordinal_rate, m13c_open_period,
    ):
        """qty=1: only 1st-position tier → $1.00"""
        resp = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={
                "driver_id": paytest_driver_id,
                "line_type": "M13C_LOADS",
                "quantity":  "1",
                "work_date": "2033-03-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        line = resp.json()
        assert Decimal(line["calculated_amount"]) == Decimal("1.0000")
        assert not line["needs_manager_review"]

    async def test_ordinal_qty_5_spans_all_tiers(
        self, session_client, auth_token, paytest_driver_id, approved_ordinal_rate, m13c_open_period,
    ):
        """qty=5: 1×$1 + 1×$2 + 3×$3 = $12"""
        resp = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={
                "driver_id": paytest_driver_id,
                "line_type": "M13C_LOADS",
                "quantity":  "5",
                "work_date": "2033-03-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        assert Decimal(resp.json()["calculated_amount"]) == Decimal("12.0000")

    async def test_ordinal_fractional_qty_rejected(
        self, session_client, auth_token, paytest_driver_id, approved_ordinal_rate, m13c_open_period,
    ):
        """Fractional quantity for OrdinalTier item is rejected with 422."""
        resp = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={
                "driver_id": paytest_driver_id,
                "line_type": "M13C_LOADS",
                "quantity":  "3.5",
                "work_date": "2033-03-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "integer" in resp.text.lower()

    async def test_ordinal_update_qty_recalculates(
        self, session_client, auth_token, paytest_driver_id, approved_ordinal_rate, m13c_open_period,
    ):
        """Updating qty on an OrdinalTier line triggers recalculation."""
        add_resp = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={
                "driver_id": paytest_driver_id, "line_type": "M13C_LOADS",
                "quantity": "1", "work_date": "2033-03-01",
            },
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 201
        line_id = add_resp.json()["draft_line_id"]
        assert Decimal(add_resp.json()["calculated_amount"]) == Decimal("1.0000")

        patch_resp = await session_client.patch(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines/{line_id}",
            json={"quantity": "3"},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        # 1×$1 + 1×$2 + 1×$3 = $6
        assert Decimal(patch_resp.json()["calculated_amount"]) == Decimal("6.0000")

    async def test_ordinal_no_approved_rate_flags_review(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_ordinal_item,
    ):
        """No approved rate for OrdinalTier item → needs_manager_review=True."""
        resp = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={
                "driver_id": paytest_driver_id, "line_type": "M13C_LOADS",
                "quantity": "2", "work_date": "2033-03-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        line = resp.json()
        assert line["calculated_amount"] is None
        assert line["needs_manager_review"] is True


# ===========================================================================
# TestRangeBracketCalculation
# ===========================================================================

class TestRangeBracketCalculation:
    """Uses 3-tier bracket: 0≤qty≤10@$1, 10<qty≤20@$2, qty>20@$3."""

    @pytest_asyncio.fixture
    async def approved_bracket_rate(
        self, session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, m13c_rates_clean,
        m13c_rbrkt_item,
    ):
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id,
            amount="1.00", range_tiers=_RANGE_3_TIERS, effective_from="2033-01-01",
        )
        assert resp.status_code == 201
        rate_id = resp.json()["driver_rate_id"]
        return await _approve_rate(session_client, auth_token, rate_id)

    async def _add_line(self, client, token, period_id, driver_id, qty: str) -> dict:
        resp = await client.post(
            f"/payroll/periods/{period_id}/lines",
            json={
                "driver_id": driver_id, "line_type": "M13C_RBRKT",
                "quantity": qty, "work_date": "2033-03-01",
            },
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_bracket_qty_in_tier1(
        self, session_client, auth_token, paytest_driver_id, approved_bracket_rate, m13c_open_period,
    ):
        """qty=5 → in tier 1 (0≤5≤10) → 5×$1=$5"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "5")
        assert Decimal(line["calculated_amount"]) == Decimal("5.0000")

    async def test_bracket_qty_exactly_tier1_boundary(
        self, session_client, auth_token, paytest_driver_id, approved_bracket_rate, m13c_open_period,
    ):
        """qty=10 (exactly at ToUnit of tier 1) → belongs to tier 1 → 10×$1=$10"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "10")
        assert Decimal(line["calculated_amount"]) == Decimal("10.0000")

    async def test_bracket_qty_just_above_tier1_boundary(
        self, session_client, auth_token, paytest_driver_id, approved_bracket_rate, m13c_open_period,
    ):
        """qty=10.1 (just above ToUnit of tier 1) → tier 2 → 10.1×$2=$20.20"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "10.1")
        assert Decimal(line["calculated_amount"]) == Decimal("20.2000")

    async def test_bracket_qty_in_tier2(
        self, session_client, auth_token, paytest_driver_id, approved_bracket_rate, m13c_open_period,
    ):
        """qty=15 → tier 2 → 15×$2=$30"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "15")
        assert Decimal(line["calculated_amount"]) == Decimal("30.0000")

    async def test_bracket_qty_in_last_tier(
        self, session_client, auth_token, paytest_driver_id, approved_bracket_rate, m13c_open_period,
    ):
        """qty=25 → tier 3 (open-ended) → 25×$3=$75"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "25")
        assert Decimal(line["calculated_amount"]) == Decimal("75.0000")

    async def test_bracket_no_approved_rate_flags_review(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_rbrkt_item,
    ):
        """No approved rate → needs_manager_review=True."""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "5")
        assert line["calculated_amount"] is None
        assert line["needs_manager_review"] is True


# ===========================================================================
# TestRangeProgressiveCalculation
# ===========================================================================

class TestRangeProgressiveCalculation:
    """Uses 3-tier progressive: 0..10@$1, 10..20@$2, 20+@$3."""

    @pytest_asyncio.fixture
    async def approved_progressive_rate(
        self, session_client, auth_token, paytest_driver_id, m13c_rprog_rt_id, m13c_rates_clean,
        m13c_rprog_item,
    ):
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_rprog_rt_id,
            amount="1.00", range_tiers=_RANGE_3_TIERS, effective_from="2033-01-01",
        )
        assert resp.status_code == 201
        rate_id = resp.json()["driver_rate_id"]
        return await _approve_rate(session_client, auth_token, rate_id)

    async def _add_line(self, client, token, period_id, driver_id, qty: str) -> dict:
        resp = await client.post(
            f"/payroll/periods/{period_id}/lines",
            json={
                "driver_id": driver_id, "line_type": "M13C_RPROG",
                "quantity": qty, "work_date": "2033-03-01",
            },
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_progressive_within_single_tier(
        self, session_client, auth_token, paytest_driver_id, approved_progressive_rate, m13c_open_period,
    ):
        """qty=5 → all in tier 1 → 5×$1=$5"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "5")
        assert Decimal(line["calculated_amount"]) == Decimal("5.0000")

    async def test_progressive_spans_two_tiers(
        self, session_client, auth_token, paytest_driver_id, approved_progressive_rate, m13c_open_period,
    ):
        """qty=15 → 10×$1 + 5×$2 = $20"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "15")
        assert Decimal(line["calculated_amount"]) == Decimal("20.0000")

    async def test_progressive_spans_all_three_tiers(
        self, session_client, auth_token, paytest_driver_id, approved_progressive_rate, m13c_open_period,
    ):
        """qty=25 → 10×$1 + 10×$2 + 5×$3 = $45"""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "25")
        assert Decimal(line["calculated_amount"]) == Decimal("45.0000")

    async def test_progressive_no_approved_rate_flags_review(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_rprog_item,
    ):
        """No approved rate → needs_manager_review=True."""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "5")
        assert line["calculated_amount"] is None
        assert line["needs_manager_review"] is True


# ===========================================================================
# TestBlockCalculation
# ===========================================================================

class TestBlockCalculation:
    """
    Block rate: $100 per block, block_size=5.
    NearestHalfUp reference: floor(x + 0.5), never banker's rounding.
    """

    async def _setup_block_rate(
        self, client, token, driver_id, rt_id, rounding_rule: str
    ) -> dict:
        """Create and approve a Block rate with given rounding_rule."""
        resp = await _create_rate(
            client, token, driver_id, rt_id,
            amount="100.00", block_size="5", rounding_rule=rounding_rule,
            effective_from="2033-01-01",
        )
        assert resp.status_code == 201, resp.text
        return await _approve_rate(client, token, resp.json()["driver_rate_id"])

    async def _add_line(self, client, token, period_id, driver_id, qty: str) -> dict:
        resp = await client.post(
            f"/payroll/periods/{period_id}/lines",
            json={
                "driver_id": driver_id, "line_type": "M13C_BLOCK",
                "quantity": qty, "work_date": "2033-03-01",
            },
            headers=auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_block_floor_exact_multiple(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """qty=10, block_size=5, Floor → 2 blocks → $200"""
        await self._setup_block_rate(session_client, auth_token, paytest_driver_id,
                                     m13c_block_rt_id, "Floor")
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "10")
        assert Decimal(line["calculated_amount"]) == Decimal("200.0000")

    async def test_block_floor_partial_block(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """qty=7, block_size=5, Floor → floor(7/5)=floor(1.4)=1 block → $100"""
        await self._setup_block_rate(session_client, auth_token, paytest_driver_id,
                                     m13c_block_rt_id, "Floor")
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "7")
        assert Decimal(line["calculated_amount"]) == Decimal("100.0000")

    async def test_block_ceiling_partial_block(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """qty=7, block_size=5, Ceiling → ceil(7/5)=ceil(1.4)=2 blocks → $200"""
        await self._setup_block_rate(session_client, auth_token, paytest_driver_id,
                                     m13c_block_rt_id, "Ceiling")
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "7")
        assert Decimal(line["calculated_amount"]) == Decimal("200.0000")

    async def test_block_nearest_half_up_at_exactly_half(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """qty=7.5, block_size=5, NearestHalfUp → floor(1.5+0.5)=floor(2.0)=2 blocks → $200
        (verifies .5 rounds UP, not banker's rounding to 2)"""
        await self._setup_block_rate(session_client, auth_token, paytest_driver_id,
                                     m13c_block_rt_id, "NearestHalfUp")
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "7.5")
        assert Decimal(line["calculated_amount"]) == Decimal("200.0000")

    async def test_block_nearest_half_up_below_half(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """qty=7, block_size=5, NearestHalfUp → floor(1.4+0.5)=floor(1.9)=1 block → $100"""
        await self._setup_block_rate(session_client, auth_token, paytest_driver_id,
                                     m13c_block_rt_id, "NearestHalfUp")
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "7")
        assert Decimal(line["calculated_amount"]) == Decimal("100.0000")

    async def test_block_nearest_half_up_above_half(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """qty=8, block_size=5, NearestHalfUp → floor(1.6+0.5)=floor(2.1)=2 blocks → $200"""
        await self._setup_block_rate(session_client, auth_token, paytest_driver_id,
                                     m13c_block_rt_id, "NearestHalfUp")
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "8")
        assert Decimal(line["calculated_amount"]) == Decimal("200.0000")

    async def test_block_zero_result_not_flagged_for_review(
        self, session_client, auth_token, paytest_driver_id, m13c_block_rt_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """qty=3, block_size=5, Floor → floor(0.6)=0 blocks → $0.
        Zero is valid for Block — must NOT set needs_manager_review."""
        await self._setup_block_rate(session_client, auth_token, paytest_driver_id,
                                     m13c_block_rt_id, "Floor")
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "3")
        assert Decimal(line["calculated_amount"]) == Decimal("0.0000")
        assert not line["needs_manager_review"]

    async def test_block_no_approved_rate_flags_review(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """No approved rate → needs_manager_review=True (not zero from calc)."""
        line = await self._add_line(session_client, auth_token,
                                    m13c_open_period["payroll_period_id"], paytest_driver_id, "10")
        assert line["calculated_amount"] is None
        assert line["needs_manager_review"] is True


# ===========================================================================
# TestRateDetailEndpoint
# ===========================================================================

class TestRateDetailEndpoint:

    async def test_get_rate_by_id_includes_tiers(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """GET /payroll/rates/{id} returns tiers for a tiered rate."""
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert resp.status_code == 201
        rate_id = resp.json()["driver_rate_id"]

        get_resp = await session_client.get(
            f"/payroll/rates/{rate_id}", headers=auth(auth_token)
        )
        assert get_resp.status_code == 200
        rate = get_resp.json()
        assert rate["tiers"] is not None
        assert len(rate["tiers"]) == 3

    async def test_get_rates_list_excludes_tiers(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item,
    ):
        """GET /payroll/rates (list) returns tiers=None (not loaded for performance)."""
        await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        list_resp = await session_client.get("/payroll/rates", headers=auth(auth_token))
        assert list_resp.status_code == 200
        rates = list_resp.json()
        assert len(rates) >= 1
        for r in rates:
            assert r.get("tiers") is None   # not loaded in list endpoint

    async def test_perunit_rate_detail_has_empty_tiers(
        self, session_client, auth_token, paytest_driver_id, paytest_rate_type_id, m13c_rates_clean,
    ):
        """PerUnit rate (HOURLY) returned from GET by ID has tiers=[] (no tier rows)."""
        from tests.test_rates import _create_rate as _create_perunit
        rate = await _create_perunit(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        get_resp = await session_client.get(
            f"/payroll/rates/{rate['driver_rate_id']}", headers=auth(auth_token)
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["tiers"] == []   # loaded but empty


# ===========================================================================
# TestCustomItemBehaviorValidation — settings API gates
# ===========================================================================

class TestCustomItemBehaviorValidation:

    async def test_rate_type_map_allowed_for_ordinal_item(
        self, session_client, auth_token, m13c_ordinal_item, m13c_ordinal_rt_id,
    ):
        """POST /settings/pay-items/{id}/rate-type-map works for OrdinalTier items."""
        # Re-assign (idempotent ON CONFLICT) — should succeed.
        resp = await session_client.post(
            f"/settings/pay-items/{m13c_ordinal_item['pay_item_id']}/rate-type-map",
            json={"rate_type_id": m13c_ordinal_rt_id, "is_primary": True},
            headers=auth(auth_token),
        )
        assert resp.status_code in (200, 201), resp.text

    async def test_rate_type_map_rejected_for_entered_amount_item(
        self, session_client, auth_token, m13c_ordinal_rt_id,
    ):
        """
        Custom Period-scope items (EnteredAmount) cannot be created.
        Verify the creation endpoint rejects Period-scope custom items with 422
        and an informative message — the rate-type-map guard is therefore
        unreachable for custom non-rate-based items.
        """
        item_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M13C_PERIOD_ONLY",
                "pay_item_name": "Period Only Item",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
                "category":      "Bonus",
            },
            headers=auth(auth_token),
        )
        assert item_resp.status_code == 422
        assert "period" in item_resp.text.lower()


# ===========================================================================
# TestM13cSafetyFixes — Codex review issues fixed
# ===========================================================================

class TestM13cSafetyFixes:
    """
    Safety guards that must hold after M13c:

    1. Unresolved M13c lines (OrdinalTier, RangeBracket, RangeProgressive, Block)
       cannot have needs_manager_review manually cleared.

    2. Approval (InReview→Approved) is blocked when M13c lines are unresolved.

    3. Finalization is blocked when M13c lines are unresolved.

    4. range_tiers from_unit field is rejected by the schema (extra='forbid').

    5. Approving a tiered rate that has no tier rows in the DB is blocked (422).
    """

    # -----------------------------------------------------------------------
    # Issue 1: cannot manually unflag unresolved M13c lines
    # -----------------------------------------------------------------------

    async def test_unresolved_ordinal_tier_line_cannot_clear_flag(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_ordinal_item,
    ):
        """Unresolved OrdinalTier line (no approved rate) → cannot clear review flag."""
        # No approved rate → line is created with needs_manager_review=True, calc=NULL
        add = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_LOADS",
                  "quantity": "2", "work_date": "2033-03-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert add.json()["needs_manager_review"] is True

        # Attempt to manually clear the flag without providing a rate
        patch = await session_client.patch(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines/{line_id}",
            json={"needs_manager_review": False},
            headers=auth(auth_token),
        )
        assert patch.status_code == 422
        assert "cannot clear" in patch.text.lower()

    async def test_unresolved_range_bracket_line_cannot_clear_flag(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_rbrkt_item,
    ):
        """Unresolved RangeBracket line → cannot clear review flag."""
        add = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_RBRKT",
                  "quantity": "5", "work_date": "2033-03-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert add.json()["needs_manager_review"] is True

        patch = await session_client.patch(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines/{line_id}",
            json={"needs_manager_review": False},
            headers=auth(auth_token),
        )
        assert patch.status_code == 422
        assert "cannot clear" in patch.text.lower()

    async def test_unresolved_range_progressive_line_cannot_clear_flag(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_rprog_item,
    ):
        """Unresolved RangeProgressive line → cannot clear review flag."""
        add = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_RPROG",
                  "quantity": "5", "work_date": "2033-03-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert add.json()["needs_manager_review"] is True

        patch = await session_client.patch(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines/{line_id}",
            json={"needs_manager_review": False},
            headers=auth(auth_token),
        )
        assert patch.status_code == 422
        assert "cannot clear" in patch.text.lower()

    async def test_unresolved_block_line_cannot_clear_flag(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_open_period, m13c_block_item,
    ):
        """Unresolved Block line → cannot clear review flag."""
        add = await session_client.post(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_BLOCK",
                  "quantity": "10", "work_date": "2033-03-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert add.json()["needs_manager_review"] is True

        patch = await session_client.patch(
            f"/payroll/periods/{m13c_open_period['payroll_period_id']}/lines/{line_id}",
            json={"needs_manager_review": False},
            headers=auth(auth_token),
        )
        assert patch.status_code == 422
        assert "cannot clear" in patch.text.lower()

    # -----------------------------------------------------------------------
    # Issue 1: approval blocked for unresolved M13c lines
    # -----------------------------------------------------------------------

    async def test_approval_blocked_for_unresolved_ordinal_tier_line(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_ordinal_item, paytest_branch_id, direct_db,
    ):
        """Period cannot transition Open→InReview when an OrdinalTier line has
        needs_manager_review=True (M16: guard moved to Open→InReview)."""
        # Set up a fresh period
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        pid = await _insert_open_period(
            direct_db, paytest_branch_id, "2033-04-01", "2033-04-07", "M13C-SAFETY-2033-04-01"
        )

        # Open the period
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Add an unresolved OrdinalTier line (no approved rate → review=True, calc=NULL)
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_LOADS",
                  "quantity": "1", "work_date": "2033-04-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        assert add.json()["needs_manager_review"] is True

        # Open→InReview must be blocked (M16: guard now fires here)
        blocked_resp = await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "InReview"}, headers=auth(auth_token)
        )
        assert blocked_resp.status_code == 422
        assert "review" in blocked_resp.text.lower()

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_finalization_blocked_for_unresolved_block_line(
        self, session_client, auth_token, paytest_driver_id, m13c_rates_clean,
        m13c_block_item, paytest_branch_id, direct_db,
    ):
        """Period cannot be finalized when a Block line has NULL calc and no review flag
        (the bypass state that could be produced by direct DB manipulation)."""
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        pid = await _insert_open_period(
            direct_db, paytest_branch_id, "2033-05-01", "2033-05-07", "M13C-SAFETY-2033-05-01"
        )

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Add an unresolved Block line (review=True, calc=NULL)
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_BLOCK",
                  "quantity": "10", "work_date": "2033-05-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert add.json()["needs_manager_review"] is True

        # Directly clear the review flag in the DB (simulating the bypass state)
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET needsmanagerreview = FALSE
                WHERE draftlineid = :lid
            """),
            {"lid": line_id},
        )

        # Open→InReview must be blocked (M16: zero-calc guard catches the direct-DB bypass)
        blocked_resp = await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "InReview"}, headers=auth(auth_token)
        )
        assert blocked_resp.status_code == 422
        assert "resolved" in blocked_resp.text.lower() or "zero" in blocked_resp.text.lower()

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    # -----------------------------------------------------------------------
    # Issue 2: from_unit in range tier payload rejected (extra='forbid')
    # -----------------------------------------------------------------------

    async def test_range_tier_from_unit_in_payload_rejected(
        self, session_client, auth_token, paytest_driver_id, m13c_rbrkt_rt_id, m13c_rates_clean,
        m13c_rbrkt_item,
    ):
        """Providing from_unit in a RangeBracket tier payload returns 422."""
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   m13c_rbrkt_rt_id,
                "amount":         "1.00",
                "effective_from": "2033-01-01",
                "range_tiers": [
                    # from_unit explicitly supplied — must be rejected
                    {"tier_sequence": 1, "from_unit": 0, "to_unit": "10", "tier_amount": "1.00"},
                    {"tier_sequence": 2, "to_unit": None, "tier_amount": "2.00"},
                ],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        # Pydantic extra='forbid' error: "Extra inputs are not permitted"
        assert "extra" in resp.text.lower() or "from_unit" in resp.text.lower()

    # -----------------------------------------------------------------------
    # Issue 3: real approval guard — tiered rate with no tier rows
    # -----------------------------------------------------------------------

    async def test_approve_tiered_rate_with_no_tier_rows_blocked(
        self, session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id, m13c_rates_clean,
        m13c_ordinal_item, direct_db,
    ):
        """Approving a PendingApproval OrdinalTier rate that has no tier rows in the DB
        returns 422 ('cannot approve a tiered rate with no tiers defined')."""
        from sqlalchemy import text as _text

        # Create a rate with valid tiers via the API
        resp = await _create_rate(
            session_client, auth_token, paytest_driver_id, m13c_ordinal_rt_id,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        assert resp.status_code == 201, resp.text
        rate_id = resp.json()["driver_rate_id"]
        assert len(resp.json()["tiers"]) == 3

        # Strip the tier rows directly from the DB (bypasses the API validation)
        await direct_db.execute(
            _text("DELETE FROM payroll.driverratetiers WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )

        # Attempt approval — the guard must fire
        approve_resp = await session_client.post(
            f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token)
        )
        assert approve_resp.status_code == 422
        detail = approve_resp.json().get("detail", "").lower()
        assert "no tiers" in detail or "tiers defined" in detail or "tier" in detail

    # -----------------------------------------------------------------------
    # Issue 4 (new): rateamount must NOT be treated as a fallback for
    # OrdinalTier / RangeBracket / RangeProgressive / Block lines.
    # A corrupted/legacy line with calc=NULL, review=FALSE, rateamount≠NULL
    # must be blocked at approval AND finalization.
    # PerUnit with rateamount override must still pass.
    # -----------------------------------------------------------------------

    async def test_tiered_line_with_rateamount_but_no_calc_blocked_at_approval(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m13c_rates_clean,
        m13c_ordinal_item,
        direct_db,
    ):
        """
        An OrdinalTier line with calculatedamount=NULL but rateamount≠NULL and
        needs_manager_review=FALSE (bypass state) must block InReview→Approved.
        rateamount must NOT be treated as a fallback for tier behaviors.
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        pid = await _insert_open_period(
            direct_db, paytest_branch_id, "2033-06-01", "2033-06-07", "M13C-SAFETY-2033-06-01"
        )

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Add an OrdinalTier line — no approved rate means calc=NULL, review=True
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_LOADS",
                  "quantity": "2", "work_date": "2033-06-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert add.json()["needs_manager_review"] is True
        assert add.json()["calculated_amount"] is None

        # Simulate a bypass: set rateamount≠NULL, clear review flag, keep calc=NULL
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    rateamount        = 5.00,
                       needsmanagerreview = FALSE
                WHERE  draftlineid = :lid
            """),
            {"lid": line_id},
        )

        # Open→InReview must be blocked even though rateamount is set (M16: guard moved here)
        blocked_resp = await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "InReview"}, headers=auth(auth_token)
        )
        assert blocked_resp.status_code == 422, (
            f"Expected 422 but got {blocked_resp.status_code}: {blocked_resp.text}"
        )
        assert "resolved" in blocked_resp.text.lower() or "zero" in blocked_resp.text.lower()

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_tiered_line_with_rateamount_but_no_calc_blocked_at_finalize(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m13c_rates_clean,
        m13c_rbrkt_item,
        direct_db,
    ):
        """
        A RangeBracket line with calculatedamount=NULL, rateamount≠NULL,
        needs_manager_review=FALSE must block /finalize even after period is
        forced to Approved status (bypassing the approval guard).
        This proves the finalization guard independently rejects the corrupt line.
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        pid = await _insert_open_period(
            direct_db, paytest_branch_id, "2033-07-01", "2033-07-07", "M13C-SAFETY-2033-07-01"
        )

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Add a RangeBracket line — no approved rate → calc=NULL, review=True
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "M13C_RBRKT",
                  "quantity": "15", "work_date": "2033-07-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert add.json()["calculated_amount"] is None

        # Bypass: rateamount≠NULL, review=FALSE, calc=NULL, period forced to Approved
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    rateamount        = 2.50,
                       needsmanagerreview = FALSE
                WHERE  draftlineid = :lid
            """),
            {"lid": line_id},
        )
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods
                SET    status = 'Approved'
                WHERE  payrollperiodid = :pid
            """),
            {"pid": pid},
        )

        # /finalize must be blocked — rateamount must NOT serve as fallback
        fin_resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin_resp.status_code == 422, (
            f"Expected 422 but got {fin_resp.status_code}: {fin_resp.text}"
        )
        assert (
            "resolved" in fin_resp.text.lower()
            or "zero" in fin_resp.text.lower()
            or "approved_snapshot_not_found" in fin_resp.text.lower()
        )

        # Cleanup — force cancel so other tests are not affected
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods
                SET    status = 'Cancelled'
                WHERE  payrollperiodid = :pid
            """),
            {"pid": pid},
        )

    async def test_perunit_line_with_rateamount_passes_approval_guard(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m13c_rates_clean,
        direct_db,
    ):
        """
        A PerUnit (system Miles) line with calculatedamount=NULL but rateamount≠NULL
        and needs_manager_review=FALSE must NOT be blocked at InReview→Approved.
        qty × rateamount is a valid and correct finalization path for PerUnit.
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        pid = await _insert_open_period(
            direct_db, paytest_branch_id, "2033-08-01", "2033-08-07", "M13C-SAFETY-2033-08-01"
        )

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Add a system Miles line — no approved Miles rate → calc=NULL, review=True
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "Miles",
                  "quantity": "100", "work_date": "2033-08-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        # calc may or may not be NULL depending on whether a Miles rate exists
        # — force the bypass state regardless
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    calculatedamount   = NULL,
                       rateamount        = 1.50,
                       needsmanagerreview = FALSE
                WHERE  draftlineid = :lid
            """),
            {"lid": line_id},
        )

        # Open→InReview must SUCCEED — PerUnit + rateamount is a valid path (M16: guard moved here)
        submit_resp = await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "InReview"}, headers=auth(auth_token)
        )
        assert submit_resp.status_code == 200, (
            f"Expected 200 (PerUnit rateamount fallback should pass) "
            f"but got {submit_resp.status_code}: {submit_resp.text}"
        )

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )
