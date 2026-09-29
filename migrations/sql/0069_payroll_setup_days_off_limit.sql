-- Enforce the two-day Normal Days Off product limit at the database.
-- Discarded Drafts are inert evidence and are not rewritten; every live Draft
-- and every Published Version must satisfy the two-day limit.

DO $$
DECLARE
    v_offenders TEXT;
BEGIN
    SELECT string_agg(
        format(
            'PayrollSetupVersionID=%s CompanyID=%s PayrollSetupID=%s LifecycleState=%s VersionNumber=%s NormalDaysOffMask=%s',
            PayrollSetupVersionID, CompanyID, PayrollSetupID, LifecycleState,
            COALESCE(VersionNumber::text, 'NULL'), NormalDaysOffMask
        ),
        '; ' ORDER BY PayrollSetupVersionID
    )
    INTO v_offenders
    FROM payroll.PayrollSetupVersions
    WHERE DiscardedAtUtc IS NULL
      AND NormalDaysOffMask IS NOT NULL
      AND length(replace((NormalDaysOffMask::integer)::bit(7)::text, '0', '')) > 2;

    IF v_offenders IS NOT NULL THEN
        RAISE EXCEPTION 'payroll_setup_days_off_limit_violation: %', v_offenders
            USING HINT = 'Published Versions are immutable and are not rewritten by this migration; '
                'edit or discard offending Drafts, and remediate Published rows only through a '
                'separately reviewed data migration or a clean database before upgrading.';
    END IF;
END $$;

ALTER TABLE payroll.PayrollSetupVersions DROP CONSTRAINT ck_PayrollSetupVersions_Mask;

ALTER TABLE payroll.PayrollSetupVersions ADD CONSTRAINT ck_PayrollSetupVersions_Mask
    CHECK (NormalDaysOffMask IS NULL OR (NormalDaysOffMask BETWEEN 0 AND 127
        AND (DiscardedAtUtc IS NOT NULL
             OR length(replace((NormalDaysOffMask::integer)::bit(7)::text, '0', '')) <= 2)));
