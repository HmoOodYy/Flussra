"""Shared Workforce state builders for HTTP integration tests.

A Driver is the permanent identity; a DriverAssignment is one home-Branch tenure.
Builders therefore never return a bare "driver id" ambiguously: they return either
the full Employee detail, a ``DriverIds`` bundle, or an explicitly named id.
"""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class DriverIds:
    """The identities one created Driver Employee carries."""

    employee_id: int
    driver_id: int
    driver_assignment_id: int
    branch_id: int


async def create_driver_employee_record(
    client: httpx.AsyncClient,
    token: str,
    *,
    branch_id: int,
    full_name: str,
    driver_number: str,
    hire_date: str | None = None,
    preferred_name: str | None = None,
    cdl_number: str | None = None,
    email: str | None = None,
    effective_from_at: str | None = None,
) -> dict:
    """Create an Employee, its permanent Driver and first assignment through Workforce."""
    driver_payload: dict = {"driver_number": driver_number, "cdl_number": cdl_number}
    if effective_from_at is not None:
        driver_payload["effective_from_at"] = effective_from_at
    payload = {
        "branch_id": branch_id,
        "full_name": full_name,
        "driver": driver_payload,
    }
    if hire_date is not None:
        payload["hire_date"] = hire_date
    if preferred_name is not None:
        payload["preferred_name"] = preferred_name
    if email is not None:
        payload["email"] = email

    response = await client.post(
        "/workforce/employees",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()


def driver_ids_from_employee(employee: dict) -> DriverIds:
    """Extract the identities from an Employee detail that has a Driver and an assignment."""
    assignment = employee["current_or_pending_assignment"]
    return DriverIds(
        employee_id=employee["employee_id"],
        driver_id=employee["driver"]["driver_id"],
        driver_assignment_id=assignment["driver_assignment_id"],
        branch_id=assignment["branch_id"],
    )


async def create_driver_ids(
    client: httpx.AsyncClient,
    token: str,
    *,
    branch_id: int,
    full_name: str,
    driver_number: str,
    hire_date: str | None = None,
    preferred_name: str | None = None,
    cdl_number: str | None = None,
    email: str | None = None,
    effective_from_at: str | None = None,
) -> DriverIds:
    """Create an Employee with a Driver and return every identity it carries."""
    employee = await create_driver_employee_record(
        client, token, branch_id=branch_id, full_name=full_name,
        driver_number=driver_number, hire_date=hire_date,
        preferred_name=preferred_name, cdl_number=cdl_number, email=email,
        effective_from_at=effective_from_at,
    )
    return driver_ids_from_employee(employee)


async def create_driver_assignment_id(
    client: httpx.AsyncClient,
    token: str,
    *,
    branch_id: int,
    full_name: str,
    driver_number: str,
    hire_date: str | None = None,
    preferred_name: str | None = None,
    cdl_number: str | None = None,
    email: str | None = None,
) -> int:
    """Create an Employee with a Driver and return its first DriverAssignmentID.

    Payroll provenance (draft lines, entries, rates) is keyed by DriverAssignmentID.
    """
    ids = await create_driver_ids(
        client, token, branch_id=branch_id, full_name=full_name,
        driver_number=driver_number, hire_date=hire_date,
        preferred_name=preferred_name, cdl_number=cdl_number, email=email,
    )
    return ids.driver_assignment_id
