"""Builders for Company Status catalog state, written directly through SQL.

The catalog belongs to the Company: a Status key has a primary name, optional aliases, a
Category, hours, the off-day semantic and an optional Status rate column; each Status rate
column owns one scalar RateDefinition with its one scalar component.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.settings.status_names import normalize_status_name


@dataclass(frozen=True)
class StatusColumn:
    status_rate_column_id: int
    rate_definition_id: int
    component_id: int


def status_column_of(cur, status_rate_column_id: int) -> StatusColumn:
    cur.execute("""
        SELECT rd.ratedefinitionid, c.ratecomponentdefinitionid
        FROM payroll.ratedefinitions rd
        JOIN payroll.ratecomponentdefinitions c ON c.ratedefinitionid = rd.ratedefinitionid
        WHERE rd.statusratecolumnid = %s
    """, (status_rate_column_id,))
    rate_definition_id, component_id = cur.fetchone()
    return StatusColumn(status_rate_column_id, rate_definition_id, component_id)


def default_status_column(cur, company_id: int) -> StatusColumn:
    """The Company's one default active Status rate column (created if absent)."""
    cur.execute("SELECT payroll.fn_EnsureDefaultStatusRateColumn(%s)", (company_id,))
    return status_column_of(cur, cur.fetchone()[0])


def make_status_column(
    cur, company_id: int, *, name: str | None = None, is_default: bool = False,
) -> StatusColumn:
    """A further Status rate column with its RateDefinition and scalar component."""
    name = "Column " + uuid4().hex[:8] if name is None else name
    cur.execute(
        "SELECT payroll.fn_CreateStatusRateColumn(%s, %s, %s, %s, NULL)",
        (company_id, name, normalize_status_name(name), is_default))
    return status_column_of(cur, cur.fetchone()[0])


def make_status_key(
    cur, company_id: int, *, name: str | None = None, category: str = "Vacation",
    hours="8", off: bool = True, column_id: int | None = None, active: bool = True,
    aliases: tuple[str, ...] = (), order: int = 0,
) -> int:
    """A Company Status key (and its aliases); returns the StatusKeyID."""
    name = "Status " + uuid4().hex[:10] if name is None else name
    code = "SK_" + uuid4().hex[:8].upper()
    cur.execute("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
             allowancecategory, hoursvalue, isoffreason, isactive, statusratecolumnid,
             displayorder)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING statuskeyid
    """, (company_id, name, normalize_status_name(name), code, code, category, hours, off,
          active, column_id, order))
    key_id = cur.fetchone()[0]
    for alias in aliases:
        add_alias(cur, key_id, alias)
    return key_id


def add_alias(cur, status_key_id: int, alias: str) -> int:
    cur.execute("""
        INSERT INTO payroll.payrollstatuskeyaliases
            (companyid, statuskeyid, isactive, aliastext, normalizedalias)
        SELECT k.companyid, k.statuskeyid, k.isactive, %s, %s
        FROM payroll.payrollstatuskeys k WHERE k.statuskeyid = %s
        RETURNING statuskeyaliasid
    """, (alias, normalize_status_name(alias), status_key_id))
    return cur.fetchone()[0]
