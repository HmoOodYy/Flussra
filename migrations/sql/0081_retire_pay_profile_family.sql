-- 0081: Retire the Pay Profile family (G0.6).
--
-- PayProfiles, PayProfilePayItems, PayProfileRates and
-- PersonPayProfileAssignments are predecessor compensation structures with no
-- surviving owner: no API, service, resolver, calculation consumer, frontend
-- surface or assignment workflow reads or writes them. Their only remaining
-- coupling to current behavior was the PayProfileRates branch of
-- core.fn_company_has_durable_monetary_state(), which made dead profile rates
-- permanently lock Company currency.
--
-- Pre-production policy: every row in these tables is disposable. Nothing is
-- migrated into DriverRates, CDPI or any future compensation structure, and no
-- archive, view or compatibility object is created.
--
-- RateTypes, DriverRates, DriverRateTiers, PayItemRateTypeMap and
-- PayItemRateSlots are NOT touched; they still have current runtime owners.
-- Employees, Drivers and Branches (referenced by the dropped assignment table)
-- are NOT touched.
--
-- Historical migrations (0001, 0076, 0080) are NOT rewritten.

-- ---------------------------------------------------------------------------
-- 1. Remove the PayProfileRates branch from the Company currency authority.
--    Every other durable monetary authority is unchanged. The function is
--    replaced in place, so the Companies currency trigger keeps using it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core.fn_company_has_durable_monetary_state(p_company_id INTEGER)
RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT
        EXISTS (SELECT 1 FROM payroll.driverrates r
                WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverratetiers t
                   JOIN payroll.driverrates r ON r.driverrateid = t.driverrateid
                   WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrollbonusevents b
                   WHERE b.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverpayrules p
                   WHERE p.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrolldraftlines d
                   WHERE d.companyid = p_company_id
                     AND (d.rateamount IS NOT NULL OR d.calculatedamount IS NOT NULL))
        OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots s
                   WHERE s.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrollfinallines f
                   WHERE f.companyid = p_company_id);
$$;

-- ---------------------------------------------------------------------------
-- 2. Drop the family in FK-safe order (children before PayProfiles). Plain
--    DROP TABLE, no CASCADE: any unexpected dependent object makes the
--    migration fail instead of being silently removed.
-- ---------------------------------------------------------------------------
DROP TABLE payroll.PersonPayProfileAssignments;
DROP TABLE payroll.PayProfileRates;
DROP TABLE payroll.PayProfilePayItems;
DROP TABLE payroll.PayProfiles;
