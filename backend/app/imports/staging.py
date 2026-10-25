"""Import-owned transient staging cleanup; no payroll source writes."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def clear_derived_state(db: AsyncConnection, cid: int, iid: int) -> None:
    # Explicit child-before-parent order; durable metadata/evidence is excluded.
    for table in ("payrollworkcells", "payrollworkrecordidentifiers", "importissues",
                  "filesourcerecords", "payrollworkrecords", "payrollworkdrivermatches", "importsourcerecords"):
        await db.execute(text(f"DELETE FROM import.{table} WHERE importid = :iid AND companyid = :cid"),
                         {"iid": iid, "cid": cid})
    await db.execute(text("""
        UPDATE import.payrollworkimports SET columnmapping = NULL, mappingfingerprint = NULL,
            contentfingerprint = NULL, planfingerprint = NULL, previewcounts = NULL
        WHERE importid = :iid AND companyid = :cid
    """), {"iid": iid, "cid": cid})
