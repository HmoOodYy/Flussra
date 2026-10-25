-- Source-record and Driver-match retirement must not scan every remaining
-- issue/identifier for each parent. These are the child sides of existing
-- composite FKs; no ownership, lifecycle or retention guard changes.
CREATE INDEX ix_ImportIssues_SourceRecordScope
    ON import.ImportIssues (SourceRecordID, ImportID, CompanyID);
CREATE INDEX ix_PayrollWorkRecordIdentifiers_MatchScope
    ON import.PayrollWorkRecordIdentifiers (DriverMatchID, ImportID, CompanyID);
