"""Import-owned PayrollWork retention; called in Payroll's finalization transaction."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.imports.staging import clear_derived_state


async def purge_payroll_work_import_details(db: AsyncConnection, *, company_id: int,
                                          period_id: int, actor_id: int) -> None:
    """Period before ordered Imports; never commit independently or swallow failures."""
    status = (await db.execute(text("""
        SELECT status FROM payroll.payrollperiods
        WHERE companyid = :cid AND payrollperiodid = :pid FOR UPDATE
    """), {"cid": company_id, "pid": period_id})).scalar_one()
    if status not in ("Locked", "Archived"):
        raise RuntimeError("PayrollWork retention requires a finalized PayrollPeriod.")
    imports = (await db.execute(text("""
        SELECT i.importid FROM import.imports i
        JOIN import.payrollworkimports w ON w.importid = i.importid AND w.companyid = i.companyid
        JOIN import.filesources f ON f.importid = i.importid AND f.companyid = i.companyid
        WHERE w.companyid = :cid AND w.payrollperiodid = :pid AND f.contentpurgedatutc IS NULL
        ORDER BY i.importid FOR UPDATE OF i
    """), {"cid": company_id, "pid": period_id})).scalars().all()
    previous = (await db.execute(text("SELECT current_setting('flussra.import_purge_id', TRUE)"))).scalar_one()
    for iid in imports:
        await db.execute(text("SELECT set_config('flussra.import_purge_id', :iid, TRUE)"), {"iid": str(iid)})
        await clear_derived_state(db, company_id, iid, reset_mapping=False)
        await db.execute(text("""
            UPDATE import.filesources SET filecontent = NULL, contentpurgedatutc = NOW(),
                inspectionmetadata = NULL
            WHERE importid = :iid AND companyid = :cid
        """), {"iid": iid, "cid": company_id})
        await db.execute(text("""
            UPDATE import.imports SET revision = revision + 1, updatedbyuserid = :uid, updatedatutc = NOW()
            WHERE importid = :iid AND companyid = :cid
        """), {"iid": iid, "cid": company_id, "uid": actor_id})
    await db.execute(text("SELECT set_config('flussra.import_purge_id', :previous, TRUE)"), {"previous": previous or ""})
