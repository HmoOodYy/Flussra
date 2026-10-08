"""Route-equivalent Submit/Resubmit whole-transaction retries against PostgreSQL."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text


async def _seed_tenant(engine, status, currency_code="USD"):
    marker = uuid4().hex[:14]
    async with engine.begin() as db:
        cid = (await db.execute(text("""
            INSERT INTO core.companies(companycode, companyname, currencycode)
            VALUES (:code, 'P3a retry tenant', :currency_code) RETURNING companyid
        """), {"code": "P3AR_" + marker, "currency_code": currency_code})).scalar_one()
        bid = (await db.execute(text("""
            INSERT INTO core.branches(companyid, branchcode, branchname)
            VALUES (:cid, :code, 'P3a retry branch') RETURNING branchid
        """), {"cid": cid, "code": "P3AR_" + marker})).scalar_one()
        company_role = (await db.execute(text("""
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
            VALUES (:cid, 'COMPANY_OWNER', 'Company Owner', 100, TRUE, TRUE, FALSE, TRUE)
            RETURNING companyroleid
        """), {"cid": cid})).scalar_one()
        await db.execute(text("""
            INSERT INTO sec.companyrolepermissions(companyroleid, permissioncode)
            SELECT :rid, permissioncode FROM sec.permissions
        """), {"rid": company_role})
        uid = (await db.execute(text("""
            INSERT INTO sec.users(companyid, username, displayname, passwordhash, isactive, canlogin)
            SELECT :cid, :name, 'P3a Test', passwordhash, TRUE, TRUE
            FROM sec.users WHERE companyid=1 AND username='admin'
            RETURNING userid
        """), {"cid": cid, "name": "p3a_" + marker})).scalar_one()
        await db.execute(text("""
            INSERT INTO sec.userbranchroles(userid, companyid, branchid, roleid, companyroleid, scopetype, isactive)
            SELECT :uid, :cid, NULL, roleid, :company_role, 'AllCompanyBranches', TRUE
            FROM sec.roles WHERE rolecode='PAYROLL_ADMIN'
        """), {"uid": uid, "cid": cid, "company_role": company_role})
        eid = (await db.execute(text("""
            INSERT INTO core.employees(companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
            VALUES (:cid, :bid, 'P3a Driver', 'Driver', 'Active', :uid) RETURNING employeeid
        """), {"cid": cid, "bid": bid, "uid": uid})).scalar_one()
        did = (await db.execute(text("""
            INSERT INTO core.drivers(companyid, branchid, employeeid, drivercode, driverstatus)
            VALUES (:cid, :bid, :eid, :code, 'Active') RETURNING driverid
        """), {"cid": cid, "bid": bid, "eid": eid, "code": "P3AR_" + marker})).scalar_one()
        pid = (await db.execute(text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, 'Open', :code, 'P3a retry period', 'Week',
                    DATE '2099-01-01', DATE '2099-01-07')
            RETURNING payrollperiodid
        """), {"cid": cid, "bid": bid, "code": "P3AR_" + marker})).scalar_one()
        if status == "Returned":
            # A schema-valid, pre-monetary Returned state lets the race exercise
            # Resubmit while Company currency is still legally changeable.
            review_id = (await db.execute(text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid, requesttype,
                     entityschema, entityname, entityid, title, description,
                     priority, status, finaldecisionbyuserid,
                     finaldecisionatutc, finaldecisionreason)
                VALUES (:cid, :bid, :uid, 'PeriodApproval',
                        'payroll', 'PayrollPeriods', :eid, 'P3a return',
                        'Pre-monetary return', 'Normal', 'Rejected',
                        :uid, NOW(), 'Test setup')
                RETURNING reviewitemid
            """), {"cid": cid, "bid": bid, "uid": uid, "eid": str(pid)})).scalar_one()
            await db.execute(text("""
                UPDATE payroll.payrollperiods
                SET status='Returned', currentreturnreviewitemid=:rid
                WHERE payrollperiodid=:pid
            """), {"rid": review_id, "pid": pid})
        await db.execute(text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                 quantity, sourcetype, sourceid, status, needsmanagerreview)
            VALUES (:cid, :bid, :pid, :did, DATE '2099-01-01', 'DailyNote',
                    1, 'User', :source, 'Active', FALSE)
        """), {"cid": cid, "bid": bid, "pid": pid, "did": did, "source": marker})
    return cid, bid, uid, pid
