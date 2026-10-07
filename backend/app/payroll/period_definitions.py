"""Payroll period definitions — shared read access to payroll.PayrollPeriodDefinitions.

PayrollPeriodDefinitions is the one immutable, period-owned definition layout: what
Company PayDefinitions exist in a Period, their frozen structural calculation
contract and their Branch applicability context. It is written once by the
canonical period creator (app.payroll.period_creation) and never updated.

Zero rows is a valid layout (a Company with no PayDefinitions, or none applicable to
the Branch). It is never a signal to read live configuration or any legacy catalog:
this module has no fallback.

This module only reads. It performs no locking and no mutation.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.definition_calculation import PeriodDefinition

_DEFINITION_SELECT = """
    SELECT ppd.payrollperioddefinitionid, ppd.payrollperiodid, ppd.companyid, ppd.branchid,
           ppd.paydefinitionid, ppd.ratedefinitionid,
           ppd.definitioncodesnapshot, ppd.definitionnamesnapshot,
           ppd.inputtypesnapshot, ppd.unitsnapshot,
           ppd.calculationmethodsnapshot, ppd.calculationmethodversionsnapshot,
           ppd.rateshapesnapshot, ppd.definitionstatusatsnapshot,
           ppd.isactiveinperiod, ppd.sortorder,
           ppd.sourcebranchconfigid, ppd.sourcebranchconfigeffectivefrom,
           ppd.sourcebranchconfigeffectiveto
    FROM   payroll.payrollperioddefinitions ppd
"""


def definition_from_row(row) -> PeriodDefinition:
    return PeriodDefinition(
        payroll_period_definition_id=int(row["payrollperioddefinitionid"]),
        pay_definition_id=int(row["paydefinitionid"]),
        rate_definition_id=int(row["ratedefinitionid"]),
        code=row["definitioncodesnapshot"],
        name=row["definitionnamesnapshot"],
        input_type=row["inputtypesnapshot"],
        unit=row["unitsnapshot"],
        calculation_method=row["calculationmethodsnapshot"],
        calculation_method_version=int(row["calculationmethodversionsnapshot"]),
        rate_shape=row["rateshapesnapshot"],
        is_active=bool(row["isactiveinperiod"]),
    )


async def list_period_definition_rows(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
    *,
    active_only: bool = False,
) -> list:
    """The period's definition snapshot rows in their frozen display order."""
    active = "AND ppd.isactiveinperiod = TRUE" if active_only else ""
    result = await db.execute(
        text(f"""
            {_DEFINITION_SELECT}
            WHERE  ppd.payrollperiodid = :pid AND ppd.companyid = :cid {active}
            ORDER  BY ppd.sortorder, ppd.payrollperioddefinitionid
        """),
        {"pid": period_id, "cid": company_id},
    )
    return list(result.mappings().all())


async def list_period_definitions(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
    *,
    active_only: bool = False,
) -> list[PeriodDefinition]:
    rows = await list_period_definition_rows(period_id, company_id, db, active_only=active_only)
    return [definition_from_row(row) for row in rows]


async def get_period_definition(
    period_id: int,
    company_id: int,
    payroll_period_definition_id: int,
    db: AsyncConnection,
) -> PeriodDefinition | None:
    """One definition of this period, or None when it does not belong to it."""
    row = (await db.execute(
        text(f"""
            {_DEFINITION_SELECT}
            WHERE  ppd.payrollperioddefinitionid = :ppd
              AND  ppd.payrollperiodid = :pid AND ppd.companyid = :cid
        """),
        {"ppd": payroll_period_definition_id, "pid": period_id, "cid": company_id},
    )).mappings().first()
    return definition_from_row(row) if row is not None else None
