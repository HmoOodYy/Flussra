# FLUSSRA C2 IMPORT DESIGN — PAYROLL WORK DATA, VERSION 1

**Status:** Draft for review, revised after an independent code review (2026-10-24). Implements `FLUSSRA_C2_IMPORT_PLAN.md`; point IDs (R, S, O, L, F, N, D) refer to that plan. Nothing here is built yet.<br>
**Scope:** importing ordinary work quantities from an `.xlsx` or `.csv` file into one Draft (Prepared), Open or Returned payroll period. Status, Bonus, People and Rate imports are out of scope (O1–O3); the engine and tables below are shaped so they can be added without a second engine (F1, F2, F7).<br>
**Prerequisite:** the Day Grid workspace (`GET /payroll/periods/{id}/entry-overview`, `frontend/src/pages/payroll/entry/`) is merged to `main` before Slice A starts. Facts in §1 marked † come from that work.

---

## 1. What the current system provides (verified in code and catalog)

| Fact | Where | Consequence for Import |
|---|---|---|
| One active ordinary value per Company × period × DriverAssignment × WorkDate × PayrollPeriodDefinition, whatever its source | `payroll.PayrollDraftLines`, `ux_draftlines_targetbusinesskey` (Status ≠ Void) | Conflicts are found in the preview, never at write time. |
| `SourceType` is `Manual` or `Import`; `update_draft_line` changes the quantity but keeps the original `SourceType`; public `POST …/lines` lets a client choose `SourceType` | `ck_draftlines_sourcetype`; `draft_line_mutation.update_draft_line`; `schemas/entry.py` `DraftLineCreate`, `routes/entry.py` | The label must be rewritten on update and forced to `Manual` on public routes (§7.4). |
| The Day Grid never stores zero (saving 0 or blank voids the line), but `POST …/lines` can create a 0 line | `day_grid.save_day_grid` `apply_quantities`; `add_draft_line` | The diff treats an existing 0 line as no value. |
| A Day Grid save row rewrites the driver-day's Status and note, runs Status limits and upserts the entry state; every `update_draft_line` writes an audit row and evidence even when the value is unchanged | `day_grid.save_day_grid`; `day_entry_state._upsert_entry_state`; `draft_line_mutation.update_draft_line` | Import must not reuse the row save: it writes quantities only, and only changed ones (§7.2). |
| A full-day-off Status day may not hold ordinary work; `FinalDayState` keeps existing quantities for definitions not submitted | `status_work_conflict.py` | The preview detects it from the held entry state; the user resolves it inside Review (§5.4); the commit removes the Status first when chosen (§7.2). |
| Status changes have their own write path with audit and evidence (`STATUS_SET`, `STATUS_CHANGED`, …) | `day_entry_state._upsert_entry_state`; `audit_evidence` action codes | Removing a Status during an import uses that path, carrying the import's correlation id (§7.2, §7.3). |
| Work on a configured (normal) day off is accepted by the Day Grid; unscheduled days never count toward Minimum/Maximum proration | `period_day_calendar.py`; plan 12A (proration) | Accepting day-off work changes no payroll rule; it is a reviewed exception recorded on the import (§5.5). |
| Driver eligibility is the period roster snapshot plus live lifecycle checks (Driver active, Employee status, hire and termination dates), with an existing-source rescue for updates and voids that `add_draft_line` turns off | `eligibility.py` (`_lifecycle_eligible_assignment_ids`, rescue, `_assert_driver_eligible_for_workdate_via_snapshot`) | The preview reproduces these exact rules (§5.2). |
| The roster of an editable period changes after creation (assignments appended, eligibility recaptured by delete and rebuild, regenerated on Draft→Open); no roster version exists | `eligibility.append_driver_assignment_to_editable_periods`, `recapture_assignment_eligibility`; period lifecycle | Staleness is detected by recomputing the plan at commit (§6), never by a stored roster version; nothing may reference eligibility rows by key. |
| A work date must be a `PayrollPeriodDays` date when the period has day rows | `period_day_calendar._validate_period_work_date` | "Inside the period" means a period day (§5.3). |
| Period definitions are written once at period creation and never updated; "operational" changes only with a code deploy | `period_definitions.py`; `is_method_operational` | The target set of a period cannot change between preview and commit. |
| Every source mutation writes immutable audit evidence with before/after line state and a `CorrelationID`; `capture_period_audit_evidence` returns the event id; evidence is linked to the submit snapshot; Bonus batches already pass a batch correlation id; `_write_line_audit` accepts a correlation id | `audit_evidence.py`; `source_evidence._capture_source_evidence` (does not pass a correlation id, discards the event id); `period_lifecycle`; `bonus.py`; `line_audit.py` | Provenance (F3) and future Undo (F5) ride on existing evidence with a small extension (§7.3). |
| `_lock_period_for_mutation` locks the period row and rechecks it is source-editable; the Company currency lock precedes it | `mutation_lock.py`; `day_grid.save_day_grid` | Commit lock order: currency → period → import (§7.1). |
| A request is one transaction that rolls back entirely on error | `app/dependencies.get_db` | A failed commit cannot persist a `Failed` status; it leaves the import in review (§3.2). |
| Unique Company-scoped identifiers: `Drivers.DriverNumber`, `Employees.EmployeeKey`; names and CDL numbers are not unique; one Driver may hold several non-overlapping DriverAssignments in one Branch period | `ux_drivers_company_drivernumber`, `ux_employees_company_employeekey`, `excl_DriverAssignments_Driver_Window` | Drivers are matched to the permanent Driver, then to the assignment of each row's date (§5.2). |
| † `GET /payroll/periods/{id}/entry-overview` returns every date's rows and configured days off | `day_grid.get_period_entry_overview` | Used by the screens; the import diff uses a lean set-based reader instead (§6). |
| `python-multipart` is installed; no Excel library; no endpoint accepts files | `backend/pyproject.toml` | Add `openpyxl` and `defusedxml`; first `UploadFile` endpoint. |
| No import permission exists | `sec.Permissions` | Import uses `payroll.entry` (it is bulk typing). |
| The `import` schema (7 tables), the `integration` schema (2 tables) and the view `app.vw_ImportBatchSummary` predate the current architecture; all tables are empty; no runtime code uses them; `integration.IntegrationSyncRuns` and the view depend on `import.ImportBatches` | catalog; `0001_initial_schema.sql`; repository search (`tests/test_durable_driver_schema.py` is the only reference) | Retired and replaced (§3.1). |

## 2. Architecture

```
file (.xlsx/.csv)
  → read to text cells (bounded, in a worker thread)          import engine
  → map columns to the target contract                         import engine
  → resolve drivers, dates and values; validate                import engine, reads payroll
  → diff against current values → plan                          import engine, reads payroll
  → user decisions (driver confirmations, conflicts, exclusions)
  → commit the plan through the quantity write path            payroll, one transaction
```

Staging never changes payroll (R9). The commit is the only payroll write and applies the same eligibility, definition, quantity and full-day-off rules as typing (R5, R6). The engine (batches, files, source rows, issues, driver matches) is generic; what is specific to payroll work data is the period target and its cells (F1).

## 3. Data model

### 3.1 Retirement of the dormant schemas (migration `0094`)
A fail-closed preflight (pattern of `0071`) raises if any of `integration.IntegrationSyncRuns`, `integration.IntegrationConnections` or the seven `import.*` tables holds a row, or if any object other than `app.vw_ImportBatchSummary` and `integration.IntegrationSyncRuns` depends on them. Then, without CASCADE: drop the view, the two integration tables and the seven import tables. Integrations get their own design when built (N5). The new tables follow. Alembic head becomes `0094`; `scripts/validate.ps1`, `scripts/compare_schema_authorities.py` (`EXPECTED_HEAD`), `README.md`, the schema guard and `tests/test_durable_driver_schema.py` follow, and a test asserts the view and old tables are gone.

### 3.2 Tables (schema `import`)
Company-scoped composite foreign keys throughout. Rows of a batch, its target, cells, issues and driver matches become immutable once the batch is `Completed` or `Cancelled` (trigger); the only later change allowed is purging file content (§3.3).

**Generic engine**
- **`import.Imports`** — `ImportID`; `CompanyID`; `BranchID` (nullable for future Company-level kinds); `ImportKind` (`PayrollWork`); `SourceKind` (`File`); `Status` (`NeedsMapping` | `NeedsReview` | `Completed` | `Cancelled`); `CorrelationID` UUID unique; `SheetName`, `HeaderRowNumber`, `DateOrder` (`DMY`|`MDY`|`YMD`|null), `DecimalSeparator` (reserved, `.` in version 1); `ColumnMapping` JSONB (source column index → target; definition targets carry both `PayrollPeriodDefinitionID` and `PayDefinitionID` so a later saved profile can be reused across periods); `MappingFingerprint`, `ContentFingerprint` (canonical hash of the mapped cell values, F6), `PlanFingerprint`; counts; `ReviewSummary` JSONB (written at commit, §3.3); created/completed/cancelled by and at.
- **`import.ImportFiles`** (1:1) — `ImportID`; `OriginalFileName`, `FileExtension`, `FileSizeBytes`, `FileSha256`; `FileContent` BYTEA nullable (≤ 10 MB); `ContentPurgedAtUtc`.
- **`import.ImportSourceRows`** — `ImportSourceRowID`; `ImportID`; `SourceRowNumber` (as the user sees it), unique per import; `RawCells` JSONB holding only mapped and identifier columns; `ImportDriverMatchID` (nullable); `IsSkipped` (no non-blank mapped quantity, e.g. a totals row); `IsExcluded`.
- **`import.ImportDriverMatches`** — one per distinct driver identifier in an import: `ImportID`; `IdentifierKind` (`DriverNumber`|`EmployeeKey`|`DriverName`); `NormalizedValue`, unique per import and kind; `DriverID` (FK `core.Drivers (DriverID, CompanyID)`, null when unresolved); `Resolution` (`ByDriverNumber`|`ByEmployeeKey`|`NameProposed`|`UserChosen`); `ConfirmedByUserID`/`ConfirmedAtUtc`. This is the structure saved aliases later reuse (L3, DWR-023).
- **`import.ImportIssues`** — `ImportID`; optional source row, cell and column; `Severity` (`Error`|`Warning`); `IssueCode`; `Message`.

**Payroll work data**
- **`import.PayrollWorkImports`** (1:1 with `Imports` of kind `PayrollWork`) — `ImportID`; `CompanyID`, `BranchID`, `PayrollPeriodID` (FK `payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID)`).
- **`import.PayrollWorkCells`** — `ImportSourceRowID` + `SourceColumnIndex` unique; `WorkDate`; `DriverAssignmentID` (FK `core.DriverAssignments (DriverAssignmentID, CompanyID, BranchID)`, resolved for the cell's date); `PayrollPeriodDefinitionID` (FK to the period's definitions); `PayDefinitionID`; `RawText`; `ParsedQuantity` NUMERIC(18,4); `IsBlank`; `Outcome` (`New`|`Change`|`Unchanged`|`Conflict`|`Invalid`); `ExpectedCurrentQuantity` (what the preview saw; null = none); flags `OnFullDayOffStatus` (with `HeldStatusKeyID`) and `OnConfiguredDayOff`; `Decision` (`UseImported`|`KeepExisting`|`Exclude`|null) and `DecisionBasisQuantity` (the existing value the decision was made against); `StatusDecision` (`RemoveStatus`|`Exclude`|null) and `DayOffDecision` (`AcceptException`|`Exclude`|null); `AppliedDraftLineID`, `AppliedAction` (`Created`|`Updated`|`Voided`), `AppliedEvidenceEventID`.
- **`import.PayrollWorkStatusRemovals`** — the full-day-off Statuses a commit removed: `ImportID`; `DriverAssignmentID`; `WorkDate`; `RemovedStatusKeyID`; `AppliedEvidenceEventID`. Part of the durable record (§3.3).

A cell carries its own `WorkDate`, so the cell model is independent of the file's layout: a one-column-per-day file (deferred, D4) is a different reader and mapping mode producing the same cells, with no change to these tables.

Nothing references `payroll.PayrollPeriodDriverEligibility` rows by key: they are deleted and rebuilt.

### 3.3 Retention (D7)
Detailed provenance (the file, its rows, cells, columns, issues and driver matches) serves Import and Review while the period can still change; it is not permanent payroll data.
- **Until finalization:** everything is kept, so any imported value can be traced to its file, sheet, row and column.
- **When the period is finalized** (in the finalize transaction, through a one-way call from the payroll lifecycle into the import module): the original file content, source rows, cells, issues and driver matches of every import of the period are deleted, including imports that were never completed.
- **Kept permanently — the durable import record:** `import.Imports` (who, when, status, correlation id, counts), `import.ImportFiles` metadata (name, size, SHA-256, `ContentPurgedAtUtc`), `import.PayrollWorkImports`, `import.PayrollWorkStatusRemovals`, and a `ReviewSummary` JSONB on `import.Imports` (conflicts resolved each way, Statuses removed, day-off exceptions accepted, cells excluded, per-definition change counts). The accepted payroll values and their audit evidence (with the import's correlation id and reason) are payroll data and stay as they are.
- The immutability trigger allows exactly this purge and nothing else.
- `RawCells` keeps only mapped and identifier columns, so unrelated personal data in a user's sheet is never stored.

## 4. Reading files and mapping

- **Accepted:** `.xlsx` and `.csv` only; `.xlsm`, `.xls` and UTF-16 CSV are refused with a clear message. The 10 MB limit is enforced while streaming the upload.
- **Safety:** `.xlsx` is read with openpyxl in read-only mode under `defusedxml`, after checking the total uncompressed size of the archive; parsing runs in a worker thread so it never blocks the server.
- **Formula cells:** the cached value is used; a formula without a computed value is an error ("formula without a calculated value"), never a blank.
- **CSV:** UTF-8 with or without BOM, else Windows-1252; delimiter sniffed among `,` `;` and tab.
- **Limits:** 20,000 data rows and 200 columns.
- **Sheet and header:** every sheet is listed; the header row is the row with the most cells matching targets, else the first mostly-text row; the user can change it.
- **Shape (version 1, D4):** one row per driver per work date. One-column-per-day files (`Mon | Tue | Wed …`) are a later reader and mapping mode that produce the same cells (§3.2); nothing in the tables, validation, plan or commit assumes the one-row-per-day layout.
- **Targets:** `DriverNumber`, `EmployeeKey`, `DriverName`, `WorkDate`, one per operational period definition, or `Ignore`. Required: at least one driver identifier, the date, and one definition. Every non-empty source column must be mapped or explicitly set to `Ignore` (D3).
- **Automatic mapping (S4):** only when a header equals, after trimming and case-folding, a target label, a definition's code or name, or the template header.
- **Numbers:** numeric cells become `Decimal` through their shortest representation (never binary float noise). Text numbers accept digits with an optional `.` fraction; anything else (thousand separators, exponents, `NaN`, signs other than a leading minus) is "Numbers only". More than 4 decimal places is an error (storage is NUMERIC(18,4)). Parsing never stops at the first bad value: every cell gets its own result.
- **Dates:** date-formatted cells are dates; a bare number under a date column is an error; text `YYYY-MM-DD` is unambiguous; for `d/m/y` versus `m/d/y` the order is inferred from values above 12 or from which order fits the period, else the user chooses it once; values with a time zone offset are refused.
- **Template (S2):** `GET …/imports/template.xlsx` builds a workbook for the period: Driver number, Driver name, Date and one column per operational definition, one row per eligible driver per period day, every text written as a string (a name starting with `=` is never a formula). Its exact headers map automatically; the hidden period stamp (L5) is deferred.

## 5. Validation (D3)

- An **error** is incoming data whose destination — driver, date, pay item, target column or any other required destination — Flussra cannot determine with confidence.
- A **decision** is understood data that cannot be accepted without the user's choice: a driver proposed by name, a conflict with an existing value (D1, D2), work on a full-day-off Status day (§5.4), work on a normal day off (§5.5).
- A **warning** is understood data that looks unusual and needs no choice.

Errors and open decisions prevent the commit. The user resolves each one or deliberately excludes the affected data and continues with the rest. Warnings never block.

### 5.1 File and mapping
Unreadable or unsafe file, limits exceeded, no header, missing required targets, two columns on one target, a non-empty column left unmapped.

### 5.2 Drivers
- Each distinct identifier becomes one driver match, resolved to a permanent Driver among the drivers on the period's roster: by `DriverNumber`, then `EmployeeKey`, then an exact normalized full name held by exactly one roster Driver (counted by Driver, not by assignment). Messages never reveal drivers or branches outside the period.
- A name match is **proposed** and needs confirmation (S5, R13); one confirmation covers every row with that identifier, and proposals can be confirmed together.
- No match, or a name shared by several Drivers → error; the user may choose the Driver for that identifier (`UserChosen`).
- Several identifier columns mapped and disagreeing on a row → error.
- A numeric identifier that may have lost leading zeros → warning.
- Per row, the DriverAssignment is the Driver's assignment eligible on the row's date, by the exact Day Grid rules: snapshot window plus live lifecycle for a new value; existing-source rescue allowed for changing or clearing a value. No eligible assignment on that date → error ("not in this payroll on …").

### 5.3 Dates and values
- An unreadable date, or a date that is not a period day → error.
- A configured (normal) day off → decision (§5.5).
- Blank → no change, never zero (R12).
- Unreadable number, negative, more than 4 decimals, or a decimal in a whole-number definition → error.
- The same driver, date and definition more than once in the file → error on every occurrence.
- A row with no non-blank mapped quantity is skipped and counted, not validated.

### 5.4 Work on a full-day-off Status day (D5)
Imported work (a non-blank, non-zero value) on a driver-day whose held Status is a full day off cannot be saved beside that Status. It is resolved inside Review; the user never has to leave Import.
- Review lists the **affected drivers once each** — no dates, no Status names, no repeated names — each with a checkbox, **none selected by default**, and a "select all" control.
- **Selecting a driver** means: on every day of the import where that driver has imported work and a full-day-off Status, remove the Status and accept the imported work (`StatusDecision = RemoveStatus` on those cells).
- **Not overwriting** means the affected imported data is not accepted: the user deliberately excludes it (`StatusDecision = Exclude`, per driver or for all remaining) and continues with the rest.
- A driver left unresolved prevents the commit.
- The impact summary states how many Statuses will be removed. A removed Status also removes any Status pay it carried; the payroll preview after commit shows the result.

### 5.5 Work on a normal day off (D6)
Imported work on a configured (normal) day off is understood but unusual. Review asks once whether to treat that work as an exception for this period.
- **Yes:** the work is accepted (`DayOffDecision = AcceptException`), and Review shows a clear notice that day-off exceptions were accepted, with their count.
- **No:** that imported data is not accepted; it is excluded (`DayOffDecision = Exclude`) and the rest of the import continues.
- Left unanswered, the import cannot be completed.
- The question covers every day-off cell of the import; individual rows can still be excluded on their own.

### 5.6 Warnings (version 1)
- A numeric identifier that may have lost leading zeros.

## 6. Plan, conflicts and staleness

The diff reads current values with a lean set-based reader (active lines, entry state, roster snapshot and one lifecycle call for the whole period), not the screen read model. For each valid, non-blank, non-excluded cell:

| Existing (an existing 0 line counts as none) | Incoming | Outcome |
|---|---|---|
| equal (numerically) | | `Unchanged` |
| none | > 0 | `New` |
| none | 0 | `Unchanged` |
| a value | different (incl. 0) | `Conflict` |

- **Decisions (D1, D2):** every conflict needs `UseImported` or `KeepExisting`, per cell or for all similar conflicts (same definition, or the whole import). Each decision records the value it was made against; if a refreshed preview shows a different existing value, the decision is cleared and the cell asks again.
- **Status and day-off decisions** (§5.4, §5.5) apply only to cells whose imported work would be written (a `New` cell, or a `Conflict` decided `UseImported`). A `StatusDecision` records the held Status it was made against; a refresh showing a different held Status clears it.
- **Plan fingerprint:** SHA-256 over every reviewed, non-excluded cell — target, incoming value, `ExpectedCurrentQuantity`, held full-day-off Status, outcome and every decision — plus the driver confirmations. Returned with the preview, sent with the commit.
- **Refresh:** an explicit refresh re-runs resolution, validation and the diff against current payroll.
- **Commit-time revalidation (S10):** under the locks the commit re-runs the same resolver, validator and diff and recomputes the plan fingerprint. A difference refuses the commit with 409 `IMPORT_PLAN_STALE`, lists the changed cells, and writes nothing; the import stays in review.
- **Same file again (S11):** an upload whose `FileSha256` or `ContentFingerprint` equals a `Completed` import of the same period is flagged ("already imported at … by …"); the diff yields no changes anyway.

## 7. Commit

### 7.1 Transaction
One request, one transaction, all or nothing (R10):
1. Authorize once (`payroll.entry`, branch access, non-driver subject); load the period; require a source-editable status.
2. Company currency lock → `_lock_period_for_mutation` → the import row `FOR UPDATE`; require `NeedsReview` (a `Completed` import returns its receipt instead: a repeated commit is idempotent). Decision writes never take the period lock after an import lock.
3. Recompute the plan (§6); refuse on difference.
4. Apply the plan through the import write path (§7.2), date by date.
5. Mark the import `Completed` with counts, applied ids and its `ReviewSummary`; return the receipt.

### 7.2 Import write path
A new payroll function applies an import plan to a pre-authorized, locked period, per driver-day, in this order:
1. **Statuses to remove** (`StatusDecision = RemoveStatus`): clear the full-day-off Status through the existing entry-state path, keeping the day's note, with its usual audit and STATUS evidence; record the removal in `import.PayrollWorkStatusRemovals`. Status limits are not involved (nothing is selected).
2. **Quantities**, through internal create/update/void variants: eligibility by the Day Grid rules (no rescue for new lines; rescue for updates and voids); `assert_no_off_status_work_conflicts` with `status_is_off` from the entry state as it stands after step 1 and only the imported definitions in `quantities`; clears first, then sets, as the Day Grid does.
- Only cells whose value changes are written. Notes, other Statuses and untouched definitions are never touched.
- The internal variants skip the per-call re-authorization and period reload the public routes need, and the evidence participant snapshot is taken once per commit. The public routes and the Day Grid keep their current behavior.

### 7.3 Provenance and evidence (F3, F5)
`_capture_source_evidence` and the STATUS evidence path accept a `correlation_id` and a `reason` and return the evidence event id. For an import, every SOURCE and STATUS event it causes carries the import's `CorrelationID` and the reason "Imported from {file name}"; before/after states stay pure line or entry state. The cell stores `AppliedEvidenceEventID` (until finalization, §3.3); the evidence events and their correlation id are permanent. `_write_line_audit` receives the same correlation id.

### 7.4 SourceType
`update_draft_line` writes `SourceType`, so the label is the source of the current value: the import writes `Import`; a manual edit of an imported value writes `Manual`. Public line routes force `Manual`. History stays in the evidence events.

## 8. API

| Method and path | Permission | Purpose |
|---|---|---|
| `POST /payroll/periods/{period_id}/imports` (multipart file) | `payroll.entry` | Create an import: store and read the file, list sheets, suggest header and mapping |
| `GET /payroll/periods/{period_id}/imports/template.xlsx` | `payroll.view` or `payroll.entry` | The period's template |
| `GET /payroll/periods/{period_id}/imports` | `payroll.view` or `payroll.entry` | Import history of the period |
| `GET /payroll/imports/{import_id}` | `payroll.view` or `payroll.entry` | Import detail: mapping, preview or receipt |
| `PUT /payroll/imports/{import_id}/mapping` | `payroll.entry` | Sheet, header row, date order, column mapping → preview |
| `PUT /payroll/imports/{import_id}/decisions` | `payroll.entry` | Driver confirmations and choices, conflict decisions, full-day-off Status decisions per driver, the day-off exception answer, exclusions → preview |
| `POST /payroll/imports/{import_id}/refresh` | `payroll.entry` | Re-run the preview against current payroll |
| `POST /payroll/imports/{import_id}/commit` (body: plan fingerprint) | `payroll.entry` | Commit (§7) |
| `POST /payroll/imports/{import_id}/cancel` | `payroll.entry` | Cancel an import not yet completed |

Every route resolves the import's period through `get_period_by_id` (Company and branch scope) and is listed in `route_inventory.py`; Driver/Self subjects are refused. An import whose period is no longer source-editable is shown as no longer committable.

## 9. Frontend
Under `frontend/src/pages/payroll/entry/import/`. The entry page gains "Add payroll data" (upload, or download the template) and an "Imports" history list. Flow: upload → sheet and header → column mapping (and date order when needed) → review (errors; driver confirmations and choices; conflicts with per-cell and bulk decisions; the full-day-off Status list of affected drivers, once each, none selected, with select all; the day-off exception question and, once accepted, its notice; warnings; row and cell exclusion) → impact summary with "Import N changes" and the number of Statuses to remove → receipt with links to the Day Grid and the payroll preview.

## 10. Build order
0. Merge the Day Grid workspace and this plan to `main`.
1. **Slice A — upload and preview, no payroll write:** migration `0094`; reading, mapping, driver matching, validation, the set-based reader and diff, preview, refresh, template; the mapping and review screens.
2. **Slice B — decisions and commit:** decisions and confirmations, the quantity write path, commit with revalidation, evidence provenance, SourceType rules, receipt and history.

## 11. Tests (behavior-named)
Reading: encodings, delimiters, limits while streaming, refused types, unsafe archives, formula without value, number grammar and precision, date order inference and refusal, header choice. Mapping: automatic only on exact matches, unmapped columns. Drivers: each resolution path, name proposals needing confirmation, disagreeing identifiers, multiple assignments of one Driver, eligibility with and without rescue, no leakage outside the period. Validation: every error, decision and warning; blank versus zero; duplicates; skipped rows. Review: the full-day-off list shows each affected driver once with nothing selected; selecting removes the Status and accepts the work, excluding drops the work, unresolved blocks; the day-off question accepts with a notice, excludes, or blocks when unanswered. Plan: the diff table, existing 0 lines, decision invalidation (values and held Statuses), fingerprints, refresh. Commit: writes exactly the decided changed cells as `Import`; removes exactly the chosen full-day-off Statuses before writing work, keeping notes; never touches other Statuses or notes; evidence carries the correlation id and the cell links the event; stale plan refused with no writes; non-editable period refused; idempotent repeat; lock order. Retention: finalizing a period purges file content and row/cell/issue/match detail of its imports and keeps the durable record and summary. Day Grid save and public line routes unchanged except the forced `Manual`. Migration: fail-closed preflight, view and old tables gone, schema-authority equivalence at `0094`. Route inventory.

## 12. Deliberate deviations from the plan
- The plan's lifecycle names `ANALYZING`, `READY`, `COMMITTING` and `FAILED` are not persisted: reading and committing each happen inside one request, a failed request rolls back, and "ready" is a computed property of a `NeedsReview` import with no blocking items.
- The template's hidden period stamp (L5) is deferred; exact headers already map the template.

## 13. Decisions
Recorded as D4–D7 in `FLUSSRA_C2_IMPORT_PLAN.md` §11 (decided 2026-10-24):
- **D4** — version 1 reads one row per driver per day; other layouts come later as reader modes (§4, §3.2).
- **D5** — work on a full-day-off Status day is resolved inside Review, per affected driver (§5.4).
- **D6** — work on a normal day off is accepted as a reviewed exception or excluded; unanswered blocks (§5.5).
- **D7** — detailed provenance and the original file are kept until finalization, then purged; the durable import record stays (§3.3).
