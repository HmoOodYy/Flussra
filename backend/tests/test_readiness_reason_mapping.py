"""Unit tests for translating Payroll Setup policy errors into readiness reasons."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.payroll_setup import readiness
from app.payroll_setup.errors import PolicyError


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy_code", "expected"), [
    ("ASSIGNMENT_NOT_FOUND", "NO_ASSIGNMENT"),
    ("VERSION_NOT_FOUND", "NO_PUBLISHED_VERSION"),
    ("AUTHORITY_BOUNDARY_CROSSING", "AUTHORITY_BOUNDARY_CONFLICT"),
    ("SETUP_NOT_ACTIVE", "SETUP_NOT_ACTIVE"),
    ("INVALID_SCHEDULE_BOUNDARY", "INVALID_SCHEDULE_BOUNDARY"),
    ("BRANCH_NOT_OPERATIONAL", "BRANCH_NOT_OPERATIONAL"),
])
async def test_readiness_reason_codes_are_normalized(monkeypatch, policy_code, expected):
    operational = MagicMock()
    operational.mappings.return_value.one_or_none.return_value = {
        "branchstatus": "Active",
        "companystatus": "Active",
        "companyissuspended": False,
    }
    db = AsyncMock()
    db.execute.return_value = operational
    resolver = AsyncMock(side_effect=PolicyError(policy_code, "synthetic readiness result"))
    monkeypatch.setattr(readiness, "resolve_payroll_setup_version", resolver)

    period_start_date = date(2090, 1, 1)
    ready, reason = await readiness.branch_schedule_readiness(
        1, 2, db, period_start_date=period_start_date,
    )

    assert ready is False
    assert reason == expected
    resolver.assert_awaited_once_with(1, 2, period_start_date, db)
