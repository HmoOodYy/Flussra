"""Payroll pay rules route handlers."""

from fastapi import APIRouter, Query

from app.payroll import (
    driver_pay_rules,
)
from app.payroll.schemas import (
    DriverPayRuleCreate,
    DriverPayRuleEnd,
    DriverPayRuleNotesUpdate,
    DriverPayRuleSummary,
)

from .dependencies import DbDep, TokenDep

router = APIRouter()


@router.get(
    "/driver-assignments/{driver_assignment_id}/pay-rules",
    response_model=list[DriverPayRuleSummary],
    summary="List pay rules (min/max) for a driver",
    responses={
        403: {"description": "No access to this driver's branch"},
        404: {"description": "Driver not found"},
    },
)
async def list_driver_pay_rules(
    driver_assignment_id: int,
    token: TokenDep,
    db: DbDep,
    rule_type: str | None = Query(None, description="MinimumPay | MaximumPay"),
    rule_status: str | None = Query(None, alias="status", description="Active | Ended | Voided"),
) -> list[DriverPayRuleSummary]:
    return await driver_pay_rules.get_driver_pay_rules(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        driver_assignment_id=driver_assignment_id,
        db=db,
        rule_type=rule_type,
        rule_status=rule_status,
    )


@router.post(
    "/driver-pay-rules",
    response_model=DriverPayRuleSummary,
    status_code=201,
    summary="Create a new driver pay rule (MinimumPay or MaximumPay)",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Driver not found"},
        422: {"description": "Overlapping rule or invalid amount"},
    },
)
async def create_driver_pay_rule(
    body: DriverPayRuleCreate,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await driver_pay_rules.create_driver_pay_rule(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.get(
    "/driver-pay-rules/{rule_id}",
    response_model=DriverPayRuleSummary,
    summary="Get a single driver pay rule by ID",
    responses={
        403: {"description": "No access to this rule's branch"},
        404: {"description": "Rule not found"},
    },
)
async def get_driver_pay_rule(
    rule_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await driver_pay_rules.get_driver_pay_rule_by_id(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/driver-pay-rules/{rule_id}/end",
    response_model=DriverPayRuleSummary,
    summary="End (close) an Active driver pay rule",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Rule not found"},
        422: {"description": "Rule not Active, or effective_to before finalized period"},
    },
)
async def end_driver_pay_rule(
    rule_id: int,
    body: DriverPayRuleEnd,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await driver_pay_rules.end_driver_pay_rule(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        effective_to=body.effective_to,
        db=db,
    )


@router.patch(
    "/driver-pay-rules/{rule_id}",
    response_model=DriverPayRuleSummary,
    summary="Update notes on a driver pay rule (notes-only)",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Rule not found"},
        422: {"description": "Rule is Voided"},
    },
)
async def update_driver_pay_rule(
    rule_id: int,
    body: DriverPayRuleNotesUpdate,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await driver_pay_rules.update_driver_pay_rule_notes(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        notes=body.notes,
        db=db,
    )


@router.post(
    "/driver-pay-rules/{rule_id}/void",
    response_model=DriverPayRuleSummary,
    summary="Void a driver pay rule (only if no finalized period governed it)",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Rule not found"},
        422: {"description": "Rule already Voided, or finalized period exists in range"},
    },
)
async def void_driver_pay_rule(
    rule_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await driver_pay_rules.void_driver_pay_rule(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
