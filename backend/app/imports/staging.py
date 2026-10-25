"""Import-owned transient staging cleanup; no payroll source writes."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def clear_derived_state(db: AsyncConnection, cid: int, iid: int, *, reset_mapping: bool = True) -> None:
    # Explicit child-before-parent order; durable metadata/evidence is excluded.
    for table in ("payrollworkcells", "payrollworkrecordidentifiers", "importissues",
                  "filesourcerecords", "payrollworkrecords", "payrollworkdrivermatches", "importsourcerecords"):
        await db.execute(text(f"DELETE FROM import.{table} WHERE importid = :iid AND companyid = :cid"),
                         {"iid": iid, "cid": cid})
    # Reinterpretation invalidates the accepted mapping/content identity. Final
    # retention preserves that history and removes only the editable plan.
    reset_identity = "columnmapping = NULL, mappingfingerprint = NULL, contentfingerprint = NULL," if reset_mapping else ""
    await db.execute(text(f"""
        UPDATE import.payrollworkimports SET {reset_identity}
            planfingerprint = NULL, previewcounts = NULL
        WHERE importid = :iid AND companyid = :cid
    """), {"iid": iid, "cid": cid})
