"""
CP-4A focused tests — pure PerUnit calculation core and production cutover.

Covers:
  - the pure core in isolation (no database);
  - exact old-vs-new Decimal parity against a local reference of the
    pre-cutover inline formula;
  - the real production caller (`add_draft_line` via the HTTP endpoint, and
    `_compute_calculated_amount` directly) now delegating to the pure core;
  - boundary non-regression for EnteredAmount/Fixed/None, which return
    before any database access in `_compute_calculated_amount` and are
    therefore exercised here directly, without database fixtures.

This is a behavior-preserving-extraction verification slice, not a new
characterization slice: `test_phase4_characterization_slice1.py` and
`test_phase4_characterization_slice2_status.py` remain the authoritative
compatibility evidence and are left unmodified. No M13c legacy-method cases
are added here (OrdinalTier/RangeBracket/RangeProgressive/Block remain out
of CP-4A scope).

Isolation: periods use year 2099 dates, distinct from every other test
module's isolation year.
"""
import contextlib
import datetime
import decimal
import uuid
from decimal import ROUND_HALF_EVEN, Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text

from app.payroll.calculation.per_unit import (
    PER_UNIT_CALCULATION_VERSION,
    PerUnitInput,
    calculate_per_unit,
)
from app.payroll.service import _compute_calculated_amount

# ---------------------------------------------------------------------------
# 1. Pure-core tests (no database)
# ---------------------------------------------------------------------------

class TestPureCorePerUnit:

    def test_ordinary_multiplication(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("6.5000"), rate_amount=Decimal("12.3400"))
        )
        assert result.calculated_amount == Decimal("80.2100")

    def test_exact_scale_result(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("6.5000"), rate_amount=Decimal("12.3400"))
        )
        assert result.calculated_amount.as_tuple().exponent == -4, (
            "Completed PerUnit amount must have exactly four decimal places"
        )

    def test_round_half_even_lower_tie_stays(self):
        # 4.0961 x 2.5000 = 10.24025000 exact; retained 4th digit EVEN (2) stays.
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("4.0961"), rate_amount=Decimal("2.5000"))
        )
        assert result.calculated_amount == Decimal("10.2402")

    def test_round_half_even_upper_tie_rounds_up(self):
        # 7.12345 x 3.0000 = 21.370350000 exact; retained 4th digit ODD (3) rounds to even (4).
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        assert result.calculated_amount == Decimal("21.3704")

    def test_full_precision_multiply_then_quantize_once(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        raw_product = Decimal("7.12345") * Decimal("3.0000")
        assert raw_product == Decimal("21.370350000"), "Full-precision product must not be pre-rounded"
        assert result.calculated_amount == raw_product.quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )

    def test_rounding_inputs_first_would_differ_from_sum_then_quantize(self):
        """
        Proves the core does NOT round-then-multiply: rounding the quantity
        to 4dp first before multiplying would silently change this result,
        demonstrating the implemented core preserves full-precision-multiply-
        then-quantize-once behavior instead.
        """
        quantity = Decimal("7.123456")
        rate = Decimal("3.0000")
        exact = calculate_per_unit(PerUnitInput(quantity=quantity, rate_amount=rate)).calculated_amount

        rounded_quantity_first = quantity.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
        wrong_order = (rounded_quantity_first * rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

        assert exact != wrong_order, (
            "This case must demonstrate that pre-rounding an input changes the "
            "result -- otherwise it does not prove multiply-then-quantize order"
        )
        assert exact == (quantity * rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

    def test_zero_quantity(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("0"), rate_amount=Decimal("99.0000"))
        )
        assert result.calculated_amount == Decimal("0.0000")

    def test_zero_rate(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("8.0000"), rate_amount=Decimal("0"))
        )
        assert result.calculated_amount == Decimal("0.0000")

    def test_negative_value_preserved_no_new_validation(self):
        """
        Characterizes current behavior only: the pure core itself performs no
        sign validation (that responsibility, if any, belongs to callers
        upstream, unchanged by CP-4A) -- a negative product passes through.
        """
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("-2.0000"), rate_amount=Decimal("5.0000"))
        )
        assert result.calculated_amount == Decimal("-10.0000")

    def test_large_but_currently_valid_decimal_values(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("999999.9999"), rate_amount=Decimal("9999.9999"))
        )
        expected = (Decimal("999999.9999") * Decimal("9999.9999")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )
        assert result.calculated_amount == expected

    def test_ambient_decimal_precision_compatibility(self):
        """
        The ambient default context (precision=28) must remain unmodified
        and sufficient for the multiplication -- confirmed by comparing
        against Python's context-free `decimal` arithmetic directly.
        """
        import decimal
        ctx = decimal.getcontext()
        assert ctx.prec == 28, "Pure core must not rely on a non-default ambient precision"
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        assert result.calculated_amount == Decimal("21.3704")

    def test_ambient_decimal_trap_compatibility(self):
        """
        Proves the core itself does not suppress an ambient Decimal trap: with
        `Inexact` enabled in a local context, a real `calculate_per_unit` call
        whose completed-line quantization genuinely loses precision
        (7.12345 x 3.0000 = 21.370350000 exact, quantized to 4dp) must raise
        -- confirming the core's single `.quantize(...)` call is not wrapped
        in any trap-suppressing logic -- and that the outer/global context's
        trap configuration is restored once the local context exits.
        """
        outer_traps_before = dict(decimal.getcontext().traps)
        with decimal.localcontext() as ctx:
            ctx.traps[decimal.Inexact] = True
            with pytest.raises(decimal.Inexact):
                calculate_per_unit(
                    PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
                )
        assert dict(decimal.getcontext().traps) == outer_traps_before, (
            "The ambient/global Decimal context's trap configuration must be "
            "unchanged after the local context used by this test exits"
        )

    def test_deterministic_repeated_calls(self):
        data = PerUnitInput(quantity=Decimal("3.25"), rate_amount=Decimal("2.0002"))
        first = calculate_per_unit(data)
        second = calculate_per_unit(data)
        assert first.calculated_amount == second.calculated_amount == Decimal("6.5006")

    def test_input_is_immutable(self):
        data = PerUnitInput(quantity=Decimal("1"), rate_amount=Decimal("1"))
        with pytest.raises(Exception):
            data.quantity = Decimal("2")  # type: ignore[misc]

    def test_result_is_immutable(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("1"), rate_amount=Decimal("1"))
        )
        with pytest.raises(Exception):
            result.calculated_amount = Decimal("2")  # type: ignore[misc]

    def test_explicit_calculation_version(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("1"), rate_amount=Decimal("1"))
        )
        assert result.calculation_version == PER_UNIT_CALCULATION_VERSION
        assert isinstance(PER_UNIT_CALCULATION_VERSION, str) and PER_UNIT_CALCULATION_VERSION

    def test_float_quantity_rejected(self):
        with pytest.raises(TypeError):
            PerUnitInput(quantity=6.5, rate_amount=Decimal("12.3400"))  # type: ignore[arg-type]

    def test_float_rate_rejected(self):
        with pytest.raises(TypeError):
            PerUnitInput(quantity=Decimal("6.5000"), rate_amount=12.34)  # type: ignore[arg-type]

    def test_no_global_decimal_context_mutation(self):
        import decimal
        before = decimal.getcontext().prec
        before_rounding = decimal.getcontext().rounding
        calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        assert decimal.getcontext().prec == before
        assert decimal.getcontext().rounding == before_rounding


# ---------------------------------------------------------------------------
# 2. Exact old-vs-new Decimal parity (local reference of the pre-cutover
#    inline formula, no database)
# ---------------------------------------------------------------------------

def _old_inline_per_unit_formula(quantity: Decimal, rate_amount: Decimal) -> Decimal:
    """
    Local reference reproducing the exact pre-cutover inline formula that
    lived at `_compute_calculated_amount` before CP-4A (see
    `test_phase4_characterization_slice1.py` / slice2 for the same
    characterized values):

        calculated = (quantity * resolved_amt).quantize(Decimal("0.0001"))
    """
    return (quantity * rate_amount).quantize(Decimal("0.0001"))


class TestOldVsNewParity:

    @pytest.mark.parametrize(
        "quantity,rate_amount",
        [
            (Decimal("6.5000"), Decimal("12.3400")),      # exact at 4dp already
            (Decimal("7.12345"), Decimal("3.0000")),       # odd retained digit -> rounds up
            (Decimal("4.0961"), Decimal("2.5000")),        # even retained digit -> stays
            (Decimal("3.25"), Decimal("2.0002")),           # Slice 2 STATUS_PAY-style case
            (Decimal("2.75"), Decimal("2.0002")),
            (Decimal("9.00"), Decimal("20.0000")),
            (Decimal("0"), Decimal("50.0000")),
            (Decimal("100.0000"), Decimal("0")),
            (Decimal("999999.9999"), Decimal("9999.9999")),
        ],
    )
    def test_exact_parity_with_old_inline_formula(self, quantity, rate_amount):
        old = _old_inline_per_unit_formula(quantity, rate_amount)
        new = calculate_per_unit(PerUnitInput(quantity=quantity, rate_amount=rate_amount)).calculated_amount
        assert new == old, f"{quantity} x {rate_amount}: old={old} new={new}"
        assert new.as_tuple().exponent == old.as_tuple().exponent == -4


# ---------------------------------------------------------------------------
# 3. Production caller parity (real HTTP endpoint + real service function)
# ---------------------------------------------------------------------------

_PERIOD_CODE_PREFIX = "P4CP4A-"
PERIOD_START = "2099-01-05"
PERIOD_END = "2099-01-11"
DATE_JAN06 = "2099-01-06"


class _DeliberateSetupFailure(Exception):
    """Test-local-only exception used by the ownership-regression tests and
    the `_inject_failure_after_write` fault-injection hooks below -- never
    referenced by production code."""


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a module-owned branch so CP4A cannot inherit another module's open slot."""
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": (code := f"CP4A_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    await session_db_conn.commit()
    assert row is not None
    return row["branchid"]


async def _cancel_active_periods(client, token, branch_id, db) -> None:
    await db.execute(
        _text(
            "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
            "WHERE branchid = :bid AND status IN ('Draft', 'Open', 'InReview', 'Returned', 'Approved') "
            "AND periodcode LIKE :prefix"
        ),
        {"bid": branch_id, "prefix": f"{_PERIOD_CODE_PREFIX}%"},
    )
    await db.commit()
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get("/payroll/periods", params={"branch_id": branch_id, "status": s}, headers=headers)
        if resp.status_code != 200:
            continue
        for p in resp.json():
            if not p.get("period_code", "").startswith(_PERIOD_CODE_PREFIX):
                continue
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )


async def _delete_period_and_children(db, period_id: int) -> None:
    """
    Removes exactly this test-owned period when it has no immutable evidence.

    Once a draft line has been written, current P6D semantics deliberately
    retain the period and its evidence.  Such a period is made terminal by
    cancellation so it cannot reserve the branch's Open slot, while its
    immutable audit history remains available for inspection.

    `add_draft_line` (service.py) unconditionally writes a real
    `PayrollDraftLines` audit row via `_write_line_audit(...,
    entity_name="PayrollDraftLines", action_code="DRAFT_LINE_ADDED",
    line_id=<DraftLineID>)` immediately after insert -- this cleanup
    captures the exact DraftLineIDs for this period BEFORE the lines
    themselves are deleted, deletes their matching audit rows by exact
    EntityID, then asserts zero residue for both the rows and their audit
    rows.
    """
    has_p6d_evidence = (await db.execute(
        _text("""
            SELECT EXISTS (
                SELECT 1 FROM payroll.payrollperiodauditevidencecoverage
                WHERE payrollperiodid = :pid
            ) OR EXISTS (
                SELECT 1 FROM payroll.payrollcalculationsnapshots
                WHERE payrollperiodid = :pid
            ) OR EXISTS (
                SELECT 1 FROM payroll.payrollperiodworkflowactionevidence
                WHERE payrollperiodid = :pid
            ) AS present
        """), {"pid": period_id},
    )).scalar_one()
    if has_p6d_evidence:
        await db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )
        await db.commit()
        return

    draft_line_ids = [
        r["draftlineid"] for r in (await db.execute(
            _text("SELECT draftlineid FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).mappings().all()
    ]
    await db.execute(
        _text("DELETE FROM audit.auditlog WHERE entityname = 'PayrollPeriods' AND entityid = :eid"),
        {"eid": str(period_id)},
    )
    if draft_line_ids:
        await db.execute(
            _text("DELETE FROM audit.auditlog WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:ids)"),
            {"ids": [str(i) for i in draft_line_ids]},
        )
    await db.execute(
        _text("DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"), {"pid": period_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"), {"pid": period_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"), {"pid": period_id},
    )

    residue = (await db.execute(
        _text("""
            SELECT
                (SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :pid) AS periods,
                (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid) AS draftlines,
                (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid) AS finallines,
                (SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'PayrollPeriods' AND entityid = :eid) AS period_audit,
                (SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:dlids)) AS draftline_audit
        """),
        {"pid": period_id, "eid": str(period_id), "dlids": [str(i) for i in draft_line_ids]},
    )).mappings().first()
    assert (
        residue["periods"] == 0 and residue["draftlines"] == 0 and residue["finallines"] == 0
        and residue["period_audit"] == 0 and residue["draftline_audit"] == 0
    ), f"Residue check failed for period {period_id}: {dict(residue)}"


async def _delete_driver_and_residue(db, driver_id: int, employee_id: int | None) -> None:
    """
    Hard-deletes exactly the test-owned driver/employee pair (never a broad
    table-wide delete) and any dependent rows scoped to this exact
    `driver_id`/`employee_id` that a caller's own cleanup (period/rate
    ownership) may not have reached yet -- idempotent: a partial setup that
    never got as far as creating a rate/period still cleans safely, since
    each DELETE is a no-op when nothing matches.

    FK order: DraftLines/FinalLines/DriverRates (reference DriverID) before
    core.Drivers, then core.Drivers (references EmployeeID) before
    core.Employees (`fk_Drivers_Employee`, migrations/sql/0001_initial_schema.sql).
    """
    await db.execute(
        _text("DELETE FROM payroll.payrollfinallines WHERE driverid = :id"), {"id": driver_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrolldraftlines WHERE driverid = :id"), {"id": driver_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.driverrates WHERE driverid = :id"), {"id": driver_id},
    )
    await db.execute(
        _text("DELETE FROM audit.auditlog WHERE entityname = 'Drivers' AND entityid = :eid"),
        {"eid": str(driver_id)},
    )
    await db.execute(
        _text("DELETE FROM core.drivers WHERE driverid = :id"), {"id": driver_id},
    )
    if employee_id is not None:
        await db.execute(
            _text("DELETE FROM audit.auditlog WHERE entityname = 'Employees' AND entityid = :eid"),
            {"eid": str(employee_id)},
        )
        await db.execute(
            _text("DELETE FROM core.employees WHERE employeeid = :id"), {"id": employee_id},
        )


@contextlib.asynccontextmanager
async def _owned_driver(session_client, auth_token, branch_id, direct_db, *, name):
    """
    Exception-safe driver lifecycle. `name` MUST be a globally-unique marker
    (the caller appends a `uuid4` suffix) chosen BEFORE this context manager
    is entered -- it is the recovery key this `finally` uses to find the
    created driver/employee even when the local `driver_id`/`employee_id`
    variables were never assigned (P1-B: a 201 response whose body then
    fails to parse, or whose `driver_id`/`employee_id` keys are missing,
    still leaves the row committed server-side; recovery here does not
    depend on the local variables reaching that assignment).
    """
    driver_id = None
    employee_id = None
    try:
        r = await session_client.post(
            "/core/drivers", json={"branch_id": branch_id, "full_name": name}, headers=auth(auth_token),
        )
        assert r.status_code == 201, f"Create driver failed: {r.text}"
        body = r.json()
        driver_id = body["driver_id"]
        employee_id = body["employee_id"]
        yield driver_id
    finally:
        if driver_id is None or employee_id is None:
            # Recovery path: the write may have committed even though the
            # local variables above were never reached.
            row = (await direct_db.execute(
                _text("""
                    SELECT d.driverid, d.employeeid
                    FROM   core.drivers d
                    JOIN   core.employees e ON e.employeeid = d.employeeid
                    WHERE  e.fullname = :name AND d.branchid = :bid
                """),
                {"name": name, "bid": branch_id},
            )).mappings().first()
            if row is not None:
                driver_id = row["driverid"]
                employee_id = row["employeeid"]
        if driver_id is not None:
            await _delete_driver_and_residue(direct_db, driver_id, employee_id)
            residue = (await direct_db.execute(
                _text("""
                    SELECT
                        (SELECT COUNT(*) FROM core.drivers WHERE driverid = :did) AS drivers,
                        (SELECT COUNT(*) FROM core.employees WHERE employeeid = :eid) AS employees,
                        (SELECT COUNT(*) FROM audit.auditlog
                            WHERE entityname = 'Drivers' AND entityid = :did_s) AS driver_audit,
                        (SELECT COUNT(*) FROM audit.auditlog
                            WHERE entityname = 'Employees' AND entityid = :eid_s) AS employee_audit
                """),
                {
                    "did": driver_id, "eid": employee_id if employee_id is not None else -1,
                    "did_s": str(driver_id), "eid_s": str(employee_id) if employee_id is not None else "-1",
                },
            )).mappings().first()
            assert residue["drivers"] == 0, f"Driver {driver_id} was not removed"
            assert residue["driver_audit"] == 0, f"Driver {driver_id} audit residue was not removed"
            if employee_id is not None:
                assert residue["employees"] == 0, f"Employee {employee_id} was not removed"
                assert residue["employee_audit"] == 0, f"Employee {employee_id} audit residue was not removed"


async def _get_hourly_rate_type_id(client, token) -> int:
    rv = await client.get("/payroll/rate-types", headers=auth(token))
    assert rv.status_code == 200
    for rt in rv.json():
        if rt["rate_code"] == "HOURLY":
            return rt["rate_type_id"]
    raise AssertionError("HOURLY rate type not found")


@contextlib.asynccontextmanager
async def _owned_driver_rate(
    direct_db, session_client, auth_token, driver_id, rate_type_id, amount, effective_from,
    *, _inject_failure_after_write: bool = False,
):
    """
    Exception-safe DriverRate lifecycle. Ownership begins in this `try`
    BEFORE the create-rate POST is sent (P1-B fix: the previous version
    assigned `rate_id = await _create_and_approve_rate(...)` OUTSIDE any
    try/finally, so a 201-then-parse-failure, or a failure in the
    subsequent approve call, never entered the `finally` at all).

    Recovery key: the exact `(driver_id, rate_type_id, effective_from)`
    tuple is unique to this test (`driver_id` is a freshly-created,
    test-owned driver), so cleanup can recover the created `DriverRateID`
    via `direct_db` even if the HTTP response was never successfully parsed.
    A unique `notes` marker (a real, schema-supported `DriverRateCreate`
    field) is also written as a second, independent recovery signal.

    `_inject_failure_after_write` is a test-local-only fault-injection
    parameter (never referenced by production code) used by the P1-B
    regression test to prove cleanup survives a failure occurring strictly
    AFTER the create-rate write commits but BEFORE `rate_id` would normally
    be extracted from the response.
    """
    marker = f"CP4A-RATE-{uuid.uuid4().hex[:8]}"
    eff_date = datetime.date.fromisoformat(effective_from)
    rate_id = None
    try:
        rc = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": driver_id, "rate_type_id": rate_type_id, "amount": amount,
                "effective_from": effective_from, "notes": marker,
            },
            headers=auth(auth_token),
        )
        assert rc.status_code == 201, f"Create rate failed: {rc.text}"
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after DriverRate write committed, before rate_id extracted"
            )
        rate_id = rc.json()["driver_rate_id"]
        ra = await session_client.post(f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token))
        assert ra.status_code == 200, f"Approve rate failed: {ra.text}"
        yield rate_id
    finally:
        if rate_id is None:
            row = (await direct_db.execute(
                _text("""
                    SELECT driverrateid FROM payroll.driverrates
                    WHERE driverid = :did AND ratetypeid = :rtid
                      AND (effectivefrom = :eff OR notes = :marker)
                """),
                {"did": driver_id, "rtid": rate_type_id, "eff": eff_date, "marker": marker},
            )).mappings().first()
            if row is not None:
                rate_id = row["driverrateid"]
        if rate_id is not None:
            await direct_db.execute(
                _text("DELETE FROM audit.auditlog WHERE entityname = 'DriverRates' AND entityid = :eid"),
                {"eid": str(rate_id)},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :id"), {"id": rate_id},
            )
            residue = (await direct_db.execute(
                _text("""
                    SELECT
                        (SELECT COUNT(*) FROM payroll.driverrates WHERE driverrateid = :id) AS rates,
                        (SELECT COUNT(*) FROM audit.auditlog
                            WHERE entityname = 'DriverRates' AND entityid = :eid) AS rate_audit
                """),
                {"id": rate_id, "eid": str(rate_id)},
            )).mappings().first()
            assert residue["rates"] == 0, f"DriverRate {rate_id} was not removed"
            assert residue["rate_audit"] == 0, f"DriverRate {rate_id} audit residue was not removed"


_PERIOD_CODE_MAX_LEN = 80  # payroll.PayrollPeriods.PeriodCode is VARCHAR(80)
_PERIOD_CODE_SEPARATOR = "-"


def _generate_unique_period_code(branch_id: int, suffix: str = "") -> str:
    """
    Builds a PeriodCode that is unique on every call regardless of whether a
    caller passes `suffix` -- a random `uuid4().hex` token is always
    present, in FULL, and is solely responsible for uniqueness (never the
    optional suffix, current time, process id, test order, or branch id
    alone), so concurrent/parallel/repeated invocations, even with an
    identical (or absent, or arbitrarily long) suffix and the same
    company/branch, can never collide.

    `PeriodCode` is `VARCHAR(80)` (migrations/sql/0001_initial_schema.sql,
    `payroll.PayrollPeriods.PeriodCode`). P1 fix (Codex fix-forward, fourth
    pass): the UUID token is reserved FIRST and never truncated -- only the
    human-readable `prefix+branch_id+suffix` portion is truncated to
    whatever space remains. The previous version built the complete string
    then applied `[:80]`, which could truncate the UUID token itself
    whenever the suffix was long enough, making two codes collide. This
    version never constructs the full string before deciding what to keep;
    it computes the exact space available for the readable portion first.
    """
    token = uuid.uuid4().hex
    reserved = len(_PERIOD_CODE_SEPARATOR) + len(token)
    max_readable_len = max(0, _PERIOD_CODE_MAX_LEN - reserved)

    fixed_part = f"{_PERIOD_CODE_PREFIX}{branch_id}"
    if len(fixed_part) > max_readable_len:
        # Pathological case (e.g. an enormous branch_id): even the fixed,
        # non-suffix identification portion alone would not fit alongside
        # the full UUID token. Truncate the fixed portion itself rather
        # than ever touching the token -- the token alone still guarantees
        # uniqueness for recovery even if the readable prefix is clipped.
        fixed_part = fixed_part[:max_readable_len]
        readable_suffix = ""
    else:
        available_for_suffix = max_readable_len - len(fixed_part)
        readable_suffix = suffix[:available_for_suffix]

    code = f"{fixed_part}{readable_suffix}{_PERIOD_CODE_SEPARATOR}{token}"
    assert len(code) <= _PERIOD_CODE_MAX_LEN, (
        f"test bug: generated PeriodCode {code!r} ({len(code)} chars) exceeds "
        f"VARCHAR({_PERIOD_CODE_MAX_LEN})"
    )
    assert code.endswith(token), (
        f"test bug: generated PeriodCode {code!r} does not end with the complete "
        f"UUID token {token!r} -- the token must never be truncated"
    )
    return code


async def _recover_period_id(db, company_id: int, branch_id: int, period_code: str) -> int | None:
    """
    Tenant-safe recovery lookup scoped by the exact
    `(CompanyID, BranchID, PeriodCode)` tuple -- the same column set as the
    real `ux_PayrollPeriods_PeriodCode_Active` unique index
    (migrations/sql/0001_initial_schema.sql: `ON payroll.PayrollPeriods
    (CompanyID, BranchID, PeriodCode) WHERE Status <> 'Cancelled'`) -- never
    PeriodCode alone, and never a broad company-wide/branch-wide/time-based/
    status-only fallback. Returns zero or one row; raises if more than one
    unexpectedly matches rather than silently cleaning multiple periods.
    """
    rows = (await db.execute(
        _text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE companyid = :cid AND branchid = :bid AND periodcode = :code
        """),
        {"cid": company_id, "bid": branch_id, "code": period_code},
    )).mappings().all()
    if len(rows) > 1:
        raise AssertionError(
            f"Recovery lookup for period {period_code!r} (company={company_id}, "
            f"branch={branch_id}) matched {len(rows)} rows; expected at most one -- "
            f"refusing to guess which to clean up"
        )
    return rows[0]["payrollperiodid"] if rows else None


@contextlib.asynccontextmanager
async def _owned_period_scenario(
    session_client, auth_token, paytest_branch_id, direct_db, *, suffix="",
    company_id: int = 1,
    _inject_failure_after_write: bool = False,
    _code_override: str | None = None,
):
    """
    Exception-safe Period lifecycle for this module's own PERIOD_START/END
    window. Ownership begins in this `try` BEFORE the INSERT is executed
    (P1-B fix: an earlier version assigned `pid = await _open_period(...)`
    OUTSIDE any try/finally).

    P1 fix (Codex third pass): the PeriodCode is now always
    `_generate_unique_period_code(...)` -- a random `uuid4` token generated
    before the INSERT -- so uniqueness never depends on the caller-supplied
    `suffix` alone; two concurrent/parallel/repeated invocations, even with
    the default empty suffix and the same company/branch, can never
    collide. Recovery in `finally` uses `_recover_period_id`, scoped by the
    exact `(company_id, paytest_branch_id, code)` tuple -- never PeriodCode
    alone -- so cleanup never depends solely on the local `pid` variable
    reaching its assignment, and never touches another test's period.

    `_inject_failure_after_write` is a test-local-only fault-injection
    parameter (never referenced by production code) used by the P1-B
    regression test to prove cleanup survives a failure occurring strictly
    AFTER the period INSERT commits but BEFORE `pid` would normally be
    extracted from the row.

    `_code_override` is a test-local-only introspection parameter (never
    referenced by production code): when supplied, it is used VERBATIM
    instead of generating a new code -- no UUID is appended to it, since its
    whole purpose is to give a failure-injection test a predeclared exact
    code to verify against. It is validated against the real
    `VARCHAR(80)` limit explicitly here rather than relying on database
    truncation.
    """
    if _code_override is not None:
        if len(_code_override) > _PERIOD_CODE_MAX_LEN:
            raise ValueError(
                f"test bug: _code_override {_code_override!r} is {len(_code_override)} "
                f"characters, exceeding PeriodCode's VARCHAR({_PERIOD_CODE_MAX_LEN}) -- "
                f"the database must never be relied on to truncate this"
            )
        code = _code_override
    else:
        code = _generate_unique_period_code(paytest_branch_id, suffix)
    pid = None
    try:
        await _cancel_active_periods(
            session_client, auth_token, paytest_branch_id, db=direct_db,
        )
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:cid, :bid, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"cid": company_id, "bid": paytest_branch_id, "code": code, "name": f"CP4A {PERIOD_START}{suffix}",
             "start": datetime.date.fromisoformat(PERIOD_START), "end": datetime.date.fromisoformat(PERIOD_END)},
        )).mappings().first()
        if _inject_failure_after_write:
            raise _DeliberateSetupFailure(
                "simulated failure after PayrollPeriod write committed, before pid extracted"
            )
        assert row is not None, f"Could not create period {code!r}"
        pid = row["payrollperiodid"]
        yield pid
    finally:
        recovered_pid = await _recover_period_id(direct_db, company_id, paytest_branch_id, code)
        if recovered_pid is not None:
            pid = recovered_pid
        if pid is not None:
            await _delete_period_and_children(direct_db, pid)


@contextlib.asynccontextmanager
async def _perunit_period(session_client, auth_token, paytest_branch_id, direct_db, *, driver_name, rate_amount, effective_from, suffix=""):
    """
    Composed single cleanup owner for the complete driver + rate + period
    integration scenario. Resources are created and their ownership entered
    in this exact order -- driver FIRST (outermost, via `_owned_driver`),
    then rate and period (inner, via `AsyncExitStack`) -- so teardown
    unwinds in the reverse, FK-safe order: period+children, then rate, then
    (only once both are gone) the driver/employee themselves. `driver_name`
    is combined with a `uuid4` suffix so it is unique enough to serve as
    `_owned_driver`'s recovery marker.
    """
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
    unique_driver_name = f"{driver_name} {uuid.uuid4().hex[:8]}"
    async with _owned_driver(
        session_client, auth_token, paytest_branch_id, direct_db, name=unique_driver_name,
    ) as driver_id:
        hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
        async with contextlib.AsyncExitStack() as stack:
            rate_id = await stack.enter_async_context(
                _owned_driver_rate(
                    direct_db, session_client, auth_token, driver_id, hourly_rt_id,
                    amount=rate_amount, effective_from=effective_from,
                )
            )
            pid = await stack.enter_async_context(
                _owned_period_scenario(session_client, auth_token, paytest_branch_id, direct_db, suffix=suffix)
            )
            yield driver_id, pid, rate_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


class TestProductionCallerPerUnit:
    """
    Exercises the real production caller chain: HTTP POST -> `add_draft_line`
    -> `_compute_calculated_amount` -> the pure PerUnit core.
    """

    @pytest.mark.asyncio
    async def test_add_draft_line_matches_pure_core(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        async with _perunit_period(
            session_client, auth_token, paytest_branch_id, direct_db,
            driver_name="CP4A PerUnit Caller", rate_amount="12.3400", effective_from=DATE_JAN06,
        ) as (driver_id, pid, rate_id):
            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": driver_id, "work_date": DATE_JAN06, "line_type": "HOURS", "quantity": "6.5000"},
                headers=auth(auth_token),
            )
            assert r.status_code == 201, f"Add HOURS line failed: {r.text}"
            line = r.json()

            expected = calculate_per_unit(
                PerUnitInput(quantity=Decimal("6.5000"), rate_amount=Decimal("12.3400"))
            ).calculated_amount
            assert Decimal(str(line["calculated_amount"])) == expected == Decimal("80.2100")
            assert line["needs_manager_review"] is False

    @pytest.mark.asyncio
    async def test_compute_calculated_amount_direct_matches_pure_core(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Calls the exact shared function every production PerUnit caller
        (`_refresh_draft_calculations`, `add_draft_line`, `update_draft_line`,
        `_compute_draft_line_preview_amounts`) invokes, proving its resolved
        rate metadata and calculated amount match the pure core directly.
        """
        async with _perunit_period(
            session_client, auth_token, paytest_branch_id, direct_db,
            driver_name="CP4A Direct Caller", rate_amount="7.1235", effective_from=DATE_JAN06,
        ) as (driver_id, pid, rate_id):
            cr = await _compute_calculated_amount(
                rate_behavior="PerUnit",
                rate_code="HOURLY",
                quantity=Decimal("2.75"),
                rate_amount_override=None,
                driver_id=driver_id,
                company_id=1,
                as_of_date=datetime.date.fromisoformat(DATE_JAN06),
                db=direct_db,
            )
            expected = calculate_per_unit(
                PerUnitInput(quantity=Decimal("2.75"), rate_amount=Decimal("7.1235"))
            ).calculated_amount
            assert cr.calculated_amount == expected
            assert cr.needs_manager_review is False
            assert cr.rate_behavior == "PerUnit"
            assert cr.resolved_rate_amount == Decimal("7.1235")


class TestScenarioOwnershipRegression:
    """
    P1 regression proofs (Codex fix-forward, second pass):

      A. `_owned_driver`'s cleanup ownership begins the instant the
         driver/employee are persisted -- before the rate or period exist --
         so a later setup failure still removes the driver.
      B. `_owned_driver_rate`'s cleanup ownership begins BEFORE the
         create-rate write is sent -- a failure strictly AFTER the write
         commits but BEFORE `rate_id` is extracted still removes the
         DriverRate (and its driver/employee).
      C. `_owned_period_scenario`'s cleanup ownership begins BEFORE the
         period INSERT is sent -- a failure strictly AFTER the write commits
         but BEFORE `pid` is extracted still removes the period (and the
         rate/driver/employee underneath it).
      D. The normal successful path leaves zero residue for every resource
         it creates -- driver, employee, DriverRate, PayrollPeriod,
         PayrollDraftLine, and each entity's AuditLog rows.
    """

    @pytest.mark.asyncio
    async def test_driver_cleanup_survives_a_setup_failure_before_rate_or_period_exist(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        marker_name = f"CP4A-DRV-FAIL-{uuid.uuid4().hex[:8]}"
        driver_id = None
        employee_id = None
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_driver(
                session_client, auth_token, paytest_branch_id, direct_db, name=marker_name,
            ) as owned_driver_id:
                driver_id = owned_driver_id
                # Confirm the driver is genuinely persisted before failing --
                # this proves the exception occurs AFTER a real, committed
                # row exists, not before creation.
                row = (await direct_db.execute(
                    _text("SELECT driverid, employeeid FROM core.drivers WHERE driverid = :id"),
                    {"id": driver_id},
                )).mappings().first()
                assert row is not None, "test bug: driver must be persisted before the deliberate failure"
                employee_id = row["employeeid"]
                # Deliberately raise before any rate or period is created --
                # i.e. before setup fully completes.
                raise _DeliberateSetupFailure("simulated failure after driver persisted, before rate/period exist")

        assert driver_id is not None and employee_id is not None, (
            "test bug: failure must be raised after the driver/employee were created"
        )
        residue = (await direct_db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM core.drivers WHERE driverid = :did) AS drivers,
                    (SELECT COUNT(*) FROM core.employees WHERE employeeid = :eid) AS employees,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'Drivers' AND entityid = :did_s) AS driver_audit,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'Employees' AND entityid = :eid_s) AS employee_audit
            """),
            {"did": driver_id, "eid": employee_id, "did_s": str(driver_id), "eid_s": str(employee_id)},
        )).mappings().first()
        assert residue["drivers"] == 0, f"Driver {driver_id} ({marker_name}) leaked after a deliberate setup failure"
        assert residue["employees"] == 0, f"Employee {employee_id} leaked after a deliberate setup failure"
        assert residue["driver_audit"] == 0, "Driver audit residue leaked after a deliberate setup failure"
        assert residue["employee_audit"] == 0, "Employee audit residue leaked after a deliberate setup failure"

    @pytest.mark.asyncio
    async def test_driver_rate_cleanup_survives_a_response_processing_failure(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Regression A (P1-B): the DriverRate write commits (201 received),
        then `_inject_failure_after_write` raises BEFORE `rate_id` is ever
        extracted from the response -- simulating a JSON-decode, key-
        extraction, or mapping failure immediately after a successful write.
        Cleanup must still find and remove the DriverRate via the
        `(driver_id, rate_type_id, effective_from)` tuple / `notes` marker
        fallback, not via the never-assigned local variable.
        """
        marker_name = f"CP4A-DRV-RATEFAIL-{uuid.uuid4().hex[:8]}"
        driver_id = None
        employee_id = None
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_driver(
                session_client, auth_token, paytest_branch_id, direct_db, name=marker_name,
            ) as owned_driver_id:
                driver_id = owned_driver_id
                row = (await direct_db.execute(
                    _text("SELECT employeeid FROM core.drivers WHERE driverid = :id"), {"id": driver_id},
                )).mappings().first()
                employee_id = row["employeeid"]
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(
                    direct_db, session_client, auth_token, driver_id, hourly_rt_id,
                    amount="15.0000", effective_from=DATE_JAN06,
                    _inject_failure_after_write=True,
                ):
                    pytest.fail("must not reach the rate context body -- injection raises first")

        assert driver_id is not None and employee_id is not None
        # Note: `_owned_driver_rate`'s own `finally` already recovered the
        # exact `rate_id` via the marker/tuple fallback and asserted its
        # rate-row AND audit-row residue BEFORE deleting it (the
        # authoritative, non-vacuous proof for P1-A/P1-B) -- if that internal
        # assertion had failed, it would have raised there and this test
        # would see a different exception type than `_DeliberateSetupFailure`,
        # failing this test. The check below re-confirms via the driver_id
        # relationship (a second, independent scoping key) that nothing
        # remains after the full unwind.
        residue = (await direct_db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.driverrates WHERE driverid = :did) AS rates,
                    (SELECT COUNT(*) FROM core.drivers WHERE driverid = :did) AS drivers,
                    (SELECT COUNT(*) FROM core.employees WHERE employeeid = :eid) AS employees
            """),
            {"did": driver_id, "eid": employee_id},
        )).mappings().first()
        assert residue["rates"] == 0, (
            f"DriverRate for driver {driver_id} leaked after a response-processing failure"
        )
        assert residue["drivers"] == 0, f"Driver {driver_id} leaked after a response-processing failure"
        assert residue["employees"] == 0, f"Employee {employee_id} leaked after a response-processing failure"

    @pytest.mark.asyncio
    async def test_period_cleanup_survives_a_response_processing_failure(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Regression B (P1-B): the PayrollPeriod write commits, then
        `_inject_failure_after_write` raises BEFORE `pid` is ever extracted
        from the row -- simulating a failure mapping the INSERT/RETURNING
        result immediately after a successful write. Cleanup must still find
        and remove the period (and everything under the outer driver/rate
        ownership) via the exact `(CompanyID, BranchID, PeriodCode)` recovery
        tuple, not via the never-assigned local variable. `_code_override`
        pre-generates the unique code so this test can verify by the exact
        same identity `_owned_period_scenario` used internally.
        """
        marker_name = f"CP4A-DRV-PERIODFAIL-{uuid.uuid4().hex[:8]}"
        code = _generate_unique_period_code(paytest_branch_id, suffix="-periodfail")
        driver_id = None
        employee_id = None
        rate_id = None
        with pytest.raises(_DeliberateSetupFailure):
            async with _owned_driver(
                session_client, auth_token, paytest_branch_id, direct_db, name=marker_name,
            ) as owned_driver_id:
                driver_id = owned_driver_id
                row = (await direct_db.execute(
                    _text("SELECT employeeid FROM core.drivers WHERE driverid = :id"), {"id": driver_id},
                )).mappings().first()
                employee_id = row["employeeid"]
                hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
                async with _owned_driver_rate(
                    direct_db, session_client, auth_token, driver_id, hourly_rt_id,
                    amount="16.0000", effective_from=DATE_JAN06,
                ) as owned_rate_id:
                    rate_id = owned_rate_id
                    async with _owned_period_scenario(
                        session_client, auth_token, paytest_branch_id, direct_db,
                        suffix="-periodfail", _inject_failure_after_write=True, _code_override=code,
                    ):
                        pytest.fail("must not reach the period context body -- injection raises first")

        assert driver_id is not None and employee_id is not None and rate_id is not None
        # Note: `_owned_period_scenario`'s own `finally` already re-derived
        # the authoritative `pid` via `_recover_period_id`'s exact
        # (company_id, branch_id, code) predicate and asserted zero
        # periods/draftlines/finallines/period_audit/draftline_audit
        # residue (via `_delete_period_and_children`) BEFORE deleting it --
        # the authoritative, non-vacuous proof for this regression. The
        # check below re-confirms via the same exact predicate and the
        # driver/rate IDs (independent scoping keys) that nothing remains
        # after the unwind.
        residue = (await direct_db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.payrollperiods
                        WHERE companyid = 1 AND branchid = :bid AND periodcode = :code) AS periods,
                    (SELECT COUNT(*) FROM payroll.driverrates WHERE driverrateid = :rid) AS rates,
                    (SELECT COUNT(*) FROM core.drivers WHERE driverid = :did) AS drivers,
                    (SELECT COUNT(*) FROM core.employees WHERE employeeid = :eid) AS employees
            """),
            {"bid": paytest_branch_id, "code": code, "rid": rate_id, "did": driver_id, "eid": employee_id},
        )).mappings().first()
        assert residue["periods"] == 0, f"Period {code!r} leaked after a response-processing failure"
        assert residue["rates"] == 0, f"DriverRate {rate_id} leaked after a response-processing failure"
        assert residue["drivers"] == 0, f"Driver {driver_id} leaked after a response-processing failure"
        assert residue["employees"] == 0, f"Employee {employee_id} leaked after a response-processing failure"

class TestPeriodCodeUniquenessAndTenantSafety:
    """
    P1 regression proofs (Codex fix-forward, third pass): `PeriodCode`
    uniqueness must never depend on a caller-supplied `suffix` alone, and
    the recovery lookup must be scoped by the exact
    `(CompanyID, BranchID, PeriodCode)` tuple, never `PeriodCode` alone.
    """

    def test_default_suffix_period_codes_are_always_distinct(self):
        """
        Two code generations with the SAME branch and the DEFAULT (empty)
        suffix must still never collide -- uniqueness comes solely from the
        random token, never from the optional suffix.
        """
        first = _generate_unique_period_code(branch_id=2)
        second = _generate_unique_period_code(branch_id=2)
        assert first != second, (
            "Two default-suffix PeriodCode generations for the same branch "
            "must never collide"
        )
        assert len(first) <= 80 and len(second) <= 80, "PeriodCode must respect VARCHAR(80)"

    @pytest.mark.asyncio
    async def test_two_default_scenarios_do_not_collide_or_cross_cancel(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Two independently-owned period scenarios, both using the DEFAULT
        empty suffix and the same company/branch, must produce distinct
        PeriodCodes and PayrollPeriodIDs, and each scenario's cleanup must
        remove only its own row.

        Run SEQUENTIALLY, not nested: the real
        `ux_PayrollPeriods_OneOpenPerBranch` unique index (`(CompanyID,
        BranchID) WHERE Status = 'Open'`) forbids two simultaneously-Open
        periods on the same branch -- an unrelated pre-existing business
        rule, not part of this PeriodCode fix -- so scenario A fully exits
        (and cleans up) before scenario B is created.
        """
        async with _owned_period_scenario(session_client, auth_token, paytest_branch_id, direct_db) as pid_a:
            code_a = (await direct_db.execute(
                _text("SELECT periodcode FROM payroll.payrollperiods WHERE payrollperiodid = :id"),
                {"id": pid_a},
            )).mappings().first()["periodcode"]
        # Scenario A has fully exited and cleaned up -- confirm its row is gone
        # before creating scenario B (same branch, same default empty suffix).
        gone_a = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.payrollperiods WHERE payrollperiodid = :id"),
            {"id": pid_a},
        )).mappings().first()
        assert gone_a["cnt"] == 0, "Scenario A's period must be removed once its own context exits"

        async with _owned_period_scenario(session_client, auth_token, paytest_branch_id, direct_db) as pid_b:
            code_b = (await direct_db.execute(
                _text("SELECT periodcode FROM payroll.payrollperiods WHERE payrollperiodid = :id"),
                {"id": pid_b},
            )).mappings().first()["periodcode"]
            # Scenario A's cleanup must not have prevented or corrupted scenario B.
            assert pid_a != pid_b, "Two default-suffix period scenarios must never share a PayrollPeriodID"
            assert code_a != code_b, "Two default-suffix period scenarios must never share a PeriodCode"
        gone_b = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.payrollperiods WHERE payrollperiodid = :id"),
            {"id": pid_b},
        )).mappings().first()
        assert gone_b["cnt"] == 0, "Scenario B's period must be removed once its own context exits"

    @pytest.mark.asyncio
    async def test_recovery_lookup_is_scoped_by_company_and_branch(
        self, paytest_branch_id: int, direct_db,
    ):
        """
        Confirms `_recover_period_id`'s predicate genuinely filters by
        CompanyID and BranchID, not PeriodCode alone: querying with the
        correct PeriodCode but a CompanyID or BranchID that does not own
        this row must return no match.
        """
        code = f"CP4A-TENANTPROBE-{uuid.uuid4().hex}"
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', :code, 'Tenant probe', 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": code, "start": datetime.date.fromisoformat(PERIOD_START),
             "end": datetime.date.fromisoformat(PERIOD_END)},
        )).mappings().first()
        pid = row["payrollperiodid"]
        try:
            wrong_company = await _recover_period_id(
                direct_db, company_id=999999, branch_id=paytest_branch_id, period_code=code,
            )
            assert wrong_company is None, (
                "Recovery must not match when CompanyID differs, even with the exact "
                "PeriodCode/BranchID"
            )
            wrong_branch = await _recover_period_id(
                direct_db, company_id=1, branch_id=999999, period_code=code,
            )
            assert wrong_branch is None, (
                "Recovery must not match when BranchID differs, even with the exact "
                "PeriodCode/CompanyID"
            )
            correct = await _recover_period_id(
                direct_db, company_id=1, branch_id=paytest_branch_id, period_code=code,
            )
            assert correct == pid, "Recovery must match when CompanyID, BranchID, and PeriodCode are all exact"
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :id"), {"id": pid},
            )

    @pytest.mark.asyncio
    async def test_recovery_raises_if_more_than_one_row_matches(
        self, paytest_branch_id: int, direct_db,
    ):
        """
        The real `ux_PayrollPeriods_PeriodCode_Active` unique index excludes
        Cancelled rows (`WHERE Status <> 'Cancelled'`), so two Cancelled
        periods can legally share the same `(CompanyID, BranchID,
        PeriodCode)` tuple. `_recover_period_id` must refuse to guess which
        one to clean up rather than silently deleting either/both.
        """
        code = f"CP4A-MULTIPROBE-{uuid.uuid4().hex}"
        ids = []
        for _ in range(2):
            row = (await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperiods
                        (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                    VALUES (1, :bid, 'Cancelled', :code, 'Multi probe', 'Week', :start, :end)
                    RETURNING payrollperiodid
                """),
                {"bid": paytest_branch_id, "code": code, "start": datetime.date.fromisoformat(PERIOD_START),
                 "end": datetime.date.fromisoformat(PERIOD_END)},
            )).mappings().first()
            ids.append(row["payrollperiodid"])
        try:
            with pytest.raises(AssertionError, match="matched 2 rows"):
                await _recover_period_id(direct_db, company_id=1, branch_id=paytest_branch_id, period_code=code)
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = ANY(:ids)"), {"ids": ids},
            )


class TestPeriodCodeUuidRetention:
    """
    P1 regression proofs (Codex fix-forward, fourth pass): a sufficiently
    long `suffix` must never truncate the UUID recovery token.
    `_generate_unique_period_code` reserves the complete `uuid4().hex` token
    (32 chars) plus a separator FIRST, then truncates only the
    human-readable `prefix+branch_id+suffix` portion to whatever space
    remains -- it never builds the full string and then applies `[:80]`.
    """

    def test_long_identical_suffix_still_produces_distinct_codes(self):
        """(A) Codex's exact repro case: two calls with the SAME branch and
        the SAME 200-character suffix must not collide."""
        long_suffix = "X" * 200
        first = _generate_unique_period_code(branch_id=2, suffix=long_suffix)
        second = _generate_unique_period_code(branch_id=2, suffix=long_suffix)
        assert first != second, (
            "Two generations with an identical 200-character suffix must still "
            "produce distinct PeriodCodes -- uniqueness must come from the UUID "
            "token, never from the suffix"
        )
        assert len(first) <= 80 and len(second) <= 80

    def test_complete_uuid_token_is_retained_with_a_long_suffix(self, monkeypatch):
        """(B) Controls the generated UUID via repository-consistent
        monkeypatching, then proves the full 32-character hex token survives
        at the end of the code even with a 200-character suffix -- no UUID
        character is ever truncated."""
        fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        monkeypatch.setattr(uuid, "uuid4", lambda: fixed_uuid)
        long_suffix = "Y" * 200
        code = _generate_unique_period_code(branch_id=2, suffix=long_suffix)
        assert code.endswith(fixed_uuid.hex), (
            f"Expected the complete UUID hex token {fixed_uuid.hex!r} at the end "
            f"of the code; got {code!r}"
        )
        assert len(code) <= 80

    @pytest.mark.parametrize(
        "suffix_len", [0, 5, 79, 80, 200],
        ids=["empty", "short", "boundary_79", "boundary_80", "over_200"],
    )
    def test_representative_suffix_lengths_never_exceed_max_and_retain_uuid(
        self, monkeypatch, suffix_len,
    ):
        """(C) Empty/short/79/80/200-character suffixes: every generated code
        must be <=80 characters and end with the complete UUID token -- no
        database-side truncation is ever required."""
        fixed_uuid = uuid.UUID("abcdefab-cdef-abcd-efab-cdefabcdefab")
        monkeypatch.setattr(uuid, "uuid4", lambda: fixed_uuid)
        suffix = "Z" * suffix_len
        code = _generate_unique_period_code(branch_id=2, suffix=suffix)
        assert len(code) <= 80, f"suffix_len={suffix_len}: code {code!r} exceeds VARCHAR(80)"
        assert code.endswith(fixed_uuid.hex), (
            f"suffix_len={suffix_len}: code {code!r} does not end with the complete UUID token"
        )

    def test_repeated_long_suffix_generations_are_all_distinct(self):
        """(D) Several codes generated with the SAME long suffix must all be
        pairwise distinct (real, non-monkeypatched UUIDs)."""
        long_suffix = "W" * 150
        codes = [_generate_unique_period_code(branch_id=2, suffix=long_suffix) for _ in range(5)]
        assert len(set(codes)) == len(codes), f"Expected 5 distinct codes; got {codes}"
        assert all(len(c) <= 80 for c in codes)

    @pytest.mark.asyncio
    async def test_code_override_preserved_exactly_when_valid(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """(E) A valid <=80-character `_code_override` is used verbatim --
        no UUID is appended to it."""
        override = f"CP4A-OVERRIDE-{uuid.uuid4().hex[:8]}"
        assert len(override) <= 80
        async with _owned_period_scenario(
            session_client, auth_token, paytest_branch_id, direct_db, _code_override=override,
        ) as pid:
            stored = (await direct_db.execute(
                _text("SELECT periodcode FROM payroll.payrollperiods WHERE payrollperiodid = :id"),
                {"id": pid},
            )).mappings().first()["periodcode"]
            assert stored == override, "_code_override must be stored verbatim, with no UUID appended"

    @pytest.mark.asyncio
    async def test_code_override_exceeding_max_length_raises_before_insertion(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """(E) An oversized `_code_override` must raise a clear test-helper
        error before any INSERT is attempted -- never rely on the database
        to truncate it."""
        oversized = "Q" * 81
        with pytest.raises(ValueError, match="exceeding PeriodCode"):
            async with _owned_period_scenario(
                session_client, auth_token, paytest_branch_id, direct_db, _code_override=oversized,
            ):
                pytest.fail("must not reach the period context body -- validation must raise first")


# ---------------------------------------------------------------------------
# 4. Boundary non-regression: EnteredAmount/Fixed/None return before any
#    database access in `_compute_calculated_amount`, so these are exercised
#    directly with no database fixtures.
# ---------------------------------------------------------------------------

class TestBoundaryNonRegressionDirect:

    @pytest.mark.asyncio
    async def test_entered_amount_passthrough_unchanged(self):
        cr = await _compute_calculated_amount(
            rate_behavior="EnteredAmount",
            rate_code=None,
            quantity=Decimal("1"),
            rate_amount_override=Decimal("55.5000"),
            driver_id=0,
            company_id=1,
            as_of_date=datetime.date(2099, 1, 6),
            db=None,
        )
        assert cr.calculated_amount == Decimal("55.5000"), (
            "EnteredAmount must pass the user-supplied amount through verbatim, unquantized"
        )
        assert cr.needs_manager_review is False

    @pytest.mark.asyncio
    async def test_fixed_returns_no_computed_amount_unchanged(self):
        cr = await _compute_calculated_amount(
            rate_behavior="Fixed",
            rate_code=None,
            quantity=Decimal("1"),
            rate_amount_override=None,
            driver_id=0,
            company_id=1,
            as_of_date=datetime.date(2099, 1, 6),
            db=None,
        )
        assert cr.calculated_amount is None
        assert cr.needs_manager_review is False

    @pytest.mark.asyncio
    async def test_none_returns_no_computed_amount_unchanged(self):
        cr = await _compute_calculated_amount(
            rate_behavior="None",
            rate_code=None,
            quantity=Decimal("1"),
            rate_amount_override=None,
            driver_id=0,
            company_id=1,
            as_of_date=datetime.date(2099, 1, 6),
            db=None,
        )
        assert cr.calculated_amount is None
        assert cr.needs_manager_review is False
