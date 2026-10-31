"""Import-owned transient staging cleanup; no payroll source writes."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def clear_derived_state(db: AsyncConnection, cid: int, iid: int, *, reset_mapping: bool = False,
                              retention: bool = False) -> None:
    """Clear only derived preview detail, retaining physical source identity."""
    params = {"iid": iid, "cid": cid}
    for table in ("payrollworkreviewdecisions", "payrollworkpreviewrulecontexts"):
        await db.execute(text(f"DELETE FROM import.{table} WHERE importid = :iid AND companyid = :cid"), params)
    for table in (
        "importissues", "payrollworkrecordidentifiers", "payrollworkrecords",
        "payrollworkpreviewgroups", "payrollworkdrivermatches", "filesourcerecords",
        "importsourcerecords",
    ):
        await db.execute(text(f"DELETE FROM import.{table} WHERE importid = :iid AND companyid = :cid"), params)
    if reset_mapping:
        await db.execute(text("""
            UPDATE import.sourcelayouts SET columnmapping = NULL, mappingfingerprint = NULL
            WHERE importid = :iid AND companyid = :cid
        """), params)
    if retention:
        await db.execute(text("""
            UPDATE import.payrollworkimports SET planfingerprint = NULL, previewcounts = NULL,
                detailpurgedatutc = NOW()
            WHERE importid = :iid AND companyid = :cid
        """), params)
    else:
        await db.execute(text("""
            UPDATE import.payrollworkimports SET mappingfingerprint = NULL, contentfingerprint = NULL,
                planfingerprint = NULL, previewcounts = NULL, reviewsummary = NULL
            WHERE importid = :iid AND companyid = :cid
        """), params)
