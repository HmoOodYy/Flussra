# FLUSSRA C2 IMPORT DESIGN — PAYROLL WORK DATA, VERSION 1

**Status:** Implementation design, with the agreed Slice A mission contract finalized on 2026-10-25. Implements [the C2 product plan](FLUSSRA_C2_IMPORT_PLAN.md); point IDs (R, S, O, L, F, N, D) refer to that plan. A1 is implemented locally and awaiting review acceptance; A2, A3 and Slice B are not implemented.<br>
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
  → read to text cells (bounded, synchronous request)        import engine
  → map columns to the target contract                         import engine
  → resolve drivers, dates and values; validate                import engine, reads payroll
  → diff against current values → plan                          import engine, reads payroll
  → user decisions (driver confirmations, conflicts, exclusions)
  → commit the plan through the quantity write path            payroll, one transaction
```

PayrollWork staging never changes payroll (R9). Its commit is the only payroll write and applies the same eligibility, definition, quantity and full-day-off rules as typing (R5, R6). The Import envelope and source-neutral records/issues are shared. File interpretation belongs to the File source; Driver matching, period mapping and payroll plan state belong to the PayrollWork target (F1). PayrollWork never creates master data. Future People Import may create/update only the Workforce entities allowed by the locked People/Workforce/Access contract, through canonical Workforce authority; it never silently creates Access/User accounts or a parallel source of truth.

Slice A is one upload/preview milestone delivered in the strict order **A1 → review → A2 → review → A3 → review**, then Slice B (§10). In Slice A, staging and preview may automatically resolve exact stable identifiers and discover/explain unresolved identities, conflicts and other required decisions. They never perform user match confirmations, choose a replacement, exclude payroll data as a user decision, remove a Status or apply a quantity. The decisions and commit steps above belong to Slice B.

### 2.1 Synchronous processing contract (D10)

Slice A completes upload inspection, interpretation, mapping and preview work within the initiating HTTP request. Blocking ZIP/XML/openpyxl/CSV work runs through bounded in-process thread offload while that same request remains pending; it must not monopolize the asyncio event loop. No queues, background import workers/jobs, polling/status APIs or asynchronous-request architecture are introduced. A pending browser request is not a persisted processing job. The two admitted File requests per application process also bound concurrent receive/inspection work; capacity exhaustion is an actionable retryable admission response, not an unbounded pending processing queue.

Perform bounded file receive/read, hashing, decompression and inspection without a retained database connection/transaction or period lock. Authenticate and authorize the caller/period in a short read transaction before application-level spreadsheet parsing or persistence, then close it. Network bytes may already have reached the server: authorization does not claim to prevent their physical arrival. Do not inject the ordinary request-lifetime `get_db()` dependency into these routes. Recheck authority and source-editable period state in the short transaction that persists staging or accepted reinterpretation. Lock the period before Import rows; never take a period lock after an Import lock. Reinterpretation copies the required bytes/version/revision in a short scoped read, parses after closing it, then checks revision and editability again under the persistence locks. This prevents recreation of detail after finalization and changes no canonical payroll fact.

The upload endpoint uses streamed request-body access and its own strict multipart receive boundary, not a normal `UploadFile` parameter alone. Accept exactly one `file` part, no additional files or form fields. Count actual body bytes before passing chunks to the parser: the route-local body maximum is the 10,000,000-byte file allowance plus 64,000 bytes of multipart overhead. Independently cap actual file bytes at 10,000,000 and actual overhead at 64,000; reject rather than truncate. Bound multipart headers and reject malformed/incomplete bodies. `Content-Length` and parser-reported sizes are not sole authorities. These bounds apply only to this route, not unrelated Flussra requests. File buffering is explicitly bounded by the file cap; any invocation-owned stream/temp resource closes on success, rejection, disconnect or parser exception.

A1 must measure representative bounded files, including a valid file near the supported upper workload (§4.1). Record file shape/size, environment and configured request/resource budgets, observed request and parsing/staging time, relevant memory and temporary-storage behavior, and whether ordinary application requests remain responsive. Include dense selected-sheet and text/decompression-heavy cases that jointly respect all limits; do not assume every independent maximum can occur in one valid file. Record measured evidence and the review conclusion with A1 validation, not a promised performance number. A2 repeats the relevant benchmark with mapping, validation and comparison enabled.

If the supported limits are unsuitable for synchronous processing, report the evidence and stop the affected mission's acceptance. An explicit design amendment is required before introducing background processing. Do not silently lower limits, truncate input, return partial results as a complete preview or build speculative asynchronous infrastructure.

#### Measured File staging workload — Python 3.12 (2026-10-25)

Run `backend/scripts/benchmark_import_files.py` from the repository root with a **Python 3.12** environment containing `backend[dev]`; the recorded run used `.artifacts/python312/Scripts/python.exe backend/scripts/benchmark_import_files.py` (on Windows, process `TEMP`/`TMP` set to `C:\Temp`, as in the validation scripts). It uses generated supported-format inputs, fresh parser processes and an invocation-owned disposable PostgreSQL database. These are bounded workload measurements, not customer-file observations or a production latency guarantee. Environment: Windows 11 build 26200, Python **3.12.10 AMD64**, Starlette 1.10.0, openpyxl 3.1.5. This canonical-runtime evidence supersedes the earlier Python 3.14 measurements. Peak working set below is the absolute parser-process peak, including its runtime, not incremental memory.

| Generated input | File bytes / actual decompressed bytes / ZIP members | Parser time | Peak working set |
|---|---|---:|---:|
| Representative XLSX, 1,000 × 10 | 32,049 / 381,492 / 9 | 0.204 s | 37.88 MB |
| Dense XLSX, 20,000 × 50, 1,000,000 cells | 2,178,190 / 33,291,342 / 9 | 16.719 s | 42.47 MB |
| Dense CSV, 20,000 × 50 | 2,260,478 / n/a / n/a | 0.371 s | 50.41 MB |
| XLSX with a hidden unselected sheet containing 5,900 cells of 8,000 text bytes each | 85,817 / 47,488,536 / 10 | 0.455 s | 37.67 MB |
| XLSX package-member boundary | 585,235 / 16,783 / 4,096 | 0.085 s | 42.96 MB |
| XLSX XML depth boundary, 256 elements | 5,030 / 18,575 / 10 | 0.007840 s | 37.07 MB |
| XLSX XML depth one over, 257 elements (refused) | 5,030 / 18,582 / 10 | 0.004698 s | 36.96 MB |

Real application staging through HTTPX ASGITransport returned 200: dense XLSX upload/inspection/persistence took **20.037984 s**, and an authenticated Day Grid GET completed in **0.175222 s while parsing was active**; text-heavy upload took **0.500990 s**, concurrent GET **0.018227 s**, also 200 during parsing. No parser-owned temp files appeared in isolated measurements. This transport exercises the application but excludes real network/proxy latency and production hardware. Repository server/proxy configuration defines no upload request-time budget; `scripts/dev_backend.ps1` launches Uvicorn without a timeout setting. The supported upper dense shape still has substantial synchronous request cost, and deployment/proxy timeout suitability remains unverified. These measurements do not approve acceptance or authorize background processing or reduced product limits. A2 adds its own staging/validation/comparison cost and must remeasure.

The internal package-member ceiling is **4,096**, over 400 times the 9–10 members observed in these ordinary, dense and text-heavy generated workbooks. The boundary package was measured as above; 4,096 is accepted and 4,097 is refused by focused tests. Ordinary generated workbook parts reached XML depth **9** (in `xl/theme/theme1.xml`), independent of dense row count; the **256-element** depth ceiling leaves over 28 times that observed hierarchy depth. Exact depth 256 was accepted and 257 refused with `IMPORT_XLSX_XML_DEPTH_LIMIT_EXCEEDED`, as measured above. These generous internal resource ceilings supplement the whole-archive 50 MB actual-byte counter and are separate from the seven product limits. XML classification includes Content Types defaults/overrides and sheet relationship targets, so non-`.xml` filenames cannot bypass checks for XML parts. Before openpyxl can perform whole-member reads, bounded stored/deflate member reads check actual inflated lengths and CRCs, rejecting understated ZIP lengths that ordinary size-clipped ZIP reads can hide. Parsing clears completed XML children as well as their text and retains only a bounded header candidate. File inspection JSON is capped at **4,000,000 UTF-8 bytes** and contains sheet/header/shape/cache counts, never all source rows.

## 3. Data model

### 3.1 Retirement of the dormant schemas (migration `0094`)
A fail-closed preflight (pattern of `0071`) raises if any of `integration.IntegrationSyncRuns`, `integration.IntegrationConnections` or the seven `import.*` tables holds a row, or if any object other than `app.vw_ImportBatchSummary` and `integration.IntegrationSyncRuns` depends on them. Then, without CASCADE: drop the view, the two integration tables and the seven import tables. Integrations get their own design when built (N5). The new tables follow. Alembic head becomes `0094`; `scripts/validate.ps1`, `scripts/compare_schema_authorities.py` (`EXPECTED_HEAD`), `README.md`, the schema guard and `tests/test_durable_driver_schema.py` follow, and a test asserts the view and old tables are gone.

### 3.2 Tables (schema `import`)
Company-scoped composite foreign keys throughout, including actor and Branch ownership where applicable. Generic core columns must not require a PayrollPeriod, Driver, spreadsheet row/column, sheet, header or file. The generic lifecycle remains `NeedsMapping`, `NeedsReview`, `Completed` and `Cancelled`; completed/cancelled envelopes and details are immutable except for narrowly controlled target retention and its revision/purge metadata. No mandatory payroll-finalized lifecycle state is added to the core.

**Generic core**
- **`import.Imports`** — `ImportID`; `CompanyID`; optional `BranchID`; `ImportKind`; `SourceKind`; lifecycle `Status`; unique `CorrelationID`; positive `Revision` (initially 1); `ContractVersion` (envelope version 1); client `IdempotencyKey` UUID unique within Company; `CreationFingerprint`; generic created/updated/completed/cancelled actors and timestamps. Kind discriminators are source/target-neutral extension points; runtime supports only PayrollWork/File in A1.
- **`import.ImportSourceRecords`** — `SourceRecordID`; `ImportID`/`CompanyID`; source-neutral `Locator` and `Payload` JSONB. No singular Driver foreign key, sheet or row requirement. Concrete File coordinates and PayrollWork meaning live in their respective contracts.
- **`import.ImportIssues`** — issue identity; `ImportID`/`CompanyID`; optional source-record reference/neutral locator; severity, code and message. Spreadsheet coordinates can be obtained from File provenance; generic issues do not require them.

**File source**
- **`import.FileSources`** (1:1 envelope/source) — `ImportID`/`CompanyID`; source `ContractVersion` (1); original filename/type/size/SHA-256; bounded `FileContent` BYTEA; `ContentPurgedAtUtc`; durable selected `SheetName`, `HeaderRowNumber`, `DateOrder` (`DMY`/`MDY`/`YMD`/null), `Encoding` and `Delimiter` (null for XLSX). Bounded `InspectionMetadata` holds editable-staging detection, header samples and counts; it is purged at finalization. Accepted interpretation remains available after that purge. Interpretation is File authority, not generic envelope state.
- **`import.FileSourceRecords`** — `SourceRecordID`/`ImportID`/`CompanyID` linked to the source-neutral record and File source; sheet and source-row coordinates, unique within that File import. Mapped File cell values are carried in the record payload only when A2 exists, not copied wholesale during A1.

**Payroll work data**
- **`import.PayrollWorkImports`** (1:1 with `Imports` of kind `PayrollWork`) — `ImportID`; `CompanyID`, `BranchID`, `PayrollPeriodID` with target composite ownership FK; target `ContractVersion` (1) and `CanonicalizationVersion` (1); durable accepted `ColumnMapping`, `MappingFingerprint` and `ContentFingerprint`; transient `PlanFingerprint` and preview counts; completed `ReviewSummary`. A2's mapping records source column index → target, including identifier roles and both `PayrollPeriodDefinitionID`/`PayDefinitionID` for definition targets, so S13/L1 can report/reuse the mapping without source rows. Mapping reuse itself remains deferred. No File requirement is imposed on the target's identity, so a later approved non-File source can use the same target contract.
- **`import.PayrollWorkRecords`** — source-record/import/company identity; mapped `RowFingerprint`; target skipped/excluded state. Payroll-specific empty-quantity and decision state is not generic source-record authority.
- **`import.PayrollWorkDriverMatches`** — import/company-scoped normalized identifier kind/value, optional permanent Driver FK, match/proposal resolution and Slice B confirmation actor/time. Saved aliases remain deferred.
- **`import.PayrollWorkRecordIdentifiers`** — source-record/import/company identity + source column index → Driver-match identity. Multiple identifier columns on one source record can point to distinct match records, so A2 can detect disagreement rather than overwrite a singular row→match reference.
- **`import.PayrollWorkCells`** — source-record/import/company/Branch/period identity + source column index; work date, DriverAssignment, period PayDefinition/RateDefinition ownership; raw/parsed quantity and blank flag; outcome/current comparison and Status/day-off flags. Independent nullable future decision domains coexist: `Decision` (`UseImported`/`KeepExisting`/`Exclude`) with typed `DecisionBasisQuantity` NUMERIC(18,4), `HeldStatusKeyID`, `StatusDecision` (`RemoveStatus`/`Exclude`) with `StatusDecisionBasisKeyID`, and `DayOffDecision` (`AcceptException`/`Exclude`). Both Status keys have Company-scoped FKs; a Status decision requires its held-Status basis, and an absent value decision cannot carry a quantity basis. Null quantity basis denotes no existing value, not zero. `Decision=Exclude` is a cell exclusion; `PayrollWorkRecords.IsExcluded` separately represents whole-row exclusion. Applied line/action/evidence stays downstream. Target composite FKs prevent crossing Import, Company, Branch, period or record boundaries. A1 creates storage only: §§5–7 govern later relevance, staleness/readiness and apply behavior; no decision endpoint or resolver is exposed now.
- **`import.PayrollWorkStatusRemovals`** — the full-day-off Statuses a commit removed: `ImportID`; `DriverAssignmentID`; `WorkDate`; `RemovedStatusKeyID`; `AppliedEvidenceEventID`. Part of the durable record (§3.3).

A cell carries its own `WorkDate`, so the cell model is independent of the file's layout: a one-column-per-day file (deferred, D4) is a different reader and mapping mode producing the same cells, with no change to these tables.

Nothing references `payroll.PayrollPeriodDriverEligibility` rows by key: they are deleted and rebuilt.

A1 creates this A2/B-ready schema with nullable/defaulted downstream state. It writes only the envelope, File source and PayrollWork target association/versions. A2 creates source records, File provenance, PayrollWork matching/record/cell/issue and fingerprint state. Slice B alone writes decisions, confirmations, applied evidence/removals and completed summaries. A1 inspection is not a mapper or preview. Do not build future People/Rate/Status persistence, a universal matcher or a generic ETL system.

### 3.2.1 Resource revision, creation identity and persisted versions

Every read returns the current `revision`. Interpretation and cancel requests require integer `expected_revision`; the short period-before-import transaction compares it under row lock and atomically increments the revision on an accepted mutation. Stale revisions return HTTP 409 with stable code `IMPORT_REVISION_CONFLICT` and no writes. Cancellation of an already cancelled import may be a no-op only with the current expected revision. Target retention also advances revision when it removes detail. A2 mapping/refresh, Slice B decision updates and commit use this same contract. Revision protects Import editing; `PlanFingerprint` separately protects content/payroll-plan staleness.

Creation requires an `Idempotency-Key` UUID header. Within the Company, the key binds the creator, target kind/source kind and versions, Branch/period, original filename and exact file bytes (SHA-256 and size). Multipart boundaries, transfer chunking and claimed MIME headers are not identity. The versioned `CreationFingerprint` is SHA-256 over canonical creation identity, not the file SHA alone. Same key/same identity returns the same Import's current bounded metadata without restoring cancelled/purged detail; different identity returns HTTP 409 `IMPORT_IDEMPOTENCY_CONFLICT`. A new key deliberately creates a second Import even for identical bytes. Always reauthorize; this is transport retry protection, distinct from later S11 completed-content duplicate protection. A retry can return already-persisted metadata after finalization, but cannot create new detail in a non-editable period.

Envelope, File source and PayrollWork target contract versions are explicitly persisted as 1. PayrollWork `CanonicalizationVersion=1` is the home for A2 row/mapping/content/plan algorithms; changing normalization requires an explicit new supported contract, never silent reinterpretation. Creation identity canonicalization is versioned by the envelope contract. Unsupported stored versions fail closed with stable `IMPORT_CONTRACT_UNSUPPORTED`, requiring reset/re-upload; no obsolete-version adapters, dual processing or speculative compatibility machinery are added.

### 3.3 Retention (D7)
For PayrollWork/File, detailed provenance serves Import and Review while the period can still change; it is not permanent payroll data. Import owns purge operations/invariants; Payroll finalization calls the PayrollWork retention hook one-way, inside its existing period-locked finalization transaction. The generic Import core does not interpret payroll finalization as a universal lifecycle event. Future source/target kinds define their own retention trigger without replacing the core.
- **Until finalization:** everything is kept, so any imported value can be traced to its file, sheet, row and column.
- **When the period is finalized** (in the finalize transaction, through a one-way call from the payroll lifecycle into the import module): the original file content, source rows, cells, issues and driver matches of every import of the period are deleted, including imports that were never completed.
- **Kept permanently — the durable PayrollWork import record:** generic envelope actors/status/correlation/revision/creation identity/versions; File metadata (name, size, SHA-256, purge timestamp) and accepted `SheetName`, `HeaderRowNumber`, `DateOrder`, `Encoding` and `Delimiter`; PayrollWork target/versions, accepted `ColumnMapping`, `MappingFingerprint`, `ContentFingerprint`, and any completed `ReviewSummary`; PayrollWork Status-removal records. Purge File bytes and editable `InspectionMetadata`, source records/File data-row coordinates, issues, PayrollWork record/match/identifier/cell details, `PlanFingerprint` and `PreviewCounts`. The durable mapping/interpretation identifies what was used for S13 and later L1; it neither retains the workbook nor implements reuse or saved aliases. Applied payroll values and permanent payroll audit evidence stay unchanged.
- The immutability trigger allows exactly this purge and nothing else.
- Retention does not clear or rewrite interpretation/mapping/semantic identity. Reinterpretation of an editable Import is a different operation: it invalidates its old mapping/content fingerprints and plan as before. Purge never generates missing mapping, completion or review data for unfinished imports. A completed/cancelled Import's durable fields remain immutable even under the retention marker; repeated purge changes neither the purge timestamp nor revision.
- A2 File payloads keep only mapped and identifier columns. The retained original file can still contain other columns and follows the same PayrollWork retention policy. Purge failure rolls back finalization and vice versa; never swallow a cleanup failure after committing payroll.

**Primary owner: A1.** The finalization cleanup hook, its transaction ordering, the permitted immutability exception and its verification must exist before any upload is exposed as an available product workflow. It covers `NeedsMapping`, `NeedsReview`, `Cancelled` and, once Slice B exists, `Completed` imports. Never-completed imports are not exempt. Keep metadata and any existing summary without fabricating completion, applied changes or a review summary for an unfinished import. After purge, metadata can explain that the period is finalized; detail cannot be reconstructed by remapping or refreshing. A3 verifies this behavior through the connected workflow; Slice B verifies completed-import provenance through the same lifecycle. This requirement adds no import apply behavior to Slice A and changes no payroll calculation, source fact or finalization outcome.

## 4. Reading files and mapping

- **Accepted:** `.xlsx` and `.csv` only; `.xlsm`, `.xls` and UTF-16 CSV are refused with a clear message. Validate actual content and its consistency with the allowed format; a filename extension or claimed MIME type alone is not sufficient. A renamed macro-enabled workbook is not an accepted `.xlsx` file.
- **Safety:** `.xlsx` is read with openpyxl in read-only mode under `defusedxml`, with archive/XML validation and the limits in §4.1. Reject duplicate/encrypted/malformed ZIP entries and invalid OOXML or renamed macro-enabled content; disable external-link retrieval. In addition to actual decompressed bytes, impose a generous internal ZIP member-count ceiling chosen and boundary-tested against representative supported workloads and measured resource behavior. Record its actual value/evidence with A1; it is a parser security guard, not the future capacity of the shared engine. Parsing follows bounded in-request thread offload (§2.1); no formulas are evaluated.
- **XML structure:** every XML/relationship part, including workbook metadata and ignored extensions, is read iteratively with a maximum root-to-leaf depth of **256 elements** (root counts as 1). Reject the 257th start element before retaining it on the parser stack, with `IMPORT_XLSX_XML_DEPTH_LIMIT_EXCEEDED`. Completed XML children are removed, not merely emptied. Fixed OOXML row/cell hierarchies do not grow deeper as row counts increase; 256 leaves a generous extension margin while bounding ancestor retention against small deeply nested parts. This internal resource guard is additional to the seven product limits and 4,096 ZIP members, and is measured/boundary-tested in §2.1.
- **Formula cells:** use the already stored cached value; Flussra does not evaluate or recalculate spreadsheet formulas, and does not promise that an existing cache is current. Preserve the distinction between a blank and a formula with no cached result. A missing required cached result is a validation error ("formula without a calculated value"), never a blank, zero or a guessed result.
- **CSV:** UTF-8 with or without BOM, else Windows-1252; delimiter sniffed among `,` `;` and tab.
- **Sheet and header:** every sheet is listed; the header row is the row with the most cells matching targets, else the first mostly-text row; the user can change it.
- **Shape (version 1, D4):** one row per driver per work date. One-column-per-day files (`Mon | Tue | Wed …`) are a later reader and mapping mode that produce the same cells (§3.2); nothing in the tables, validation, plan or commit assumes the one-row-per-day layout.
- **Targets:** `DriverNumber`, `EmployeeKey`, `DriverName`, `WorkDate`, one per operational period definition, or `Ignore`. Required: at least one driver identifier, the date, and one definition. Every non-empty source column must be mapped or explicitly set to `Ignore` (D3).
- **Automatic mapping (S4):** only when a header equals, after trimming and case-folding, a target label, a definition's code or name, or the template header.
- **Numbers:** numeric cells become `Decimal` through their shortest representation (never binary float noise). Text numbers accept digits with an optional `.` fraction; anything else (thousand separators, exponents, `NaN`, signs other than a leading minus) is "Numbers only". More than 4 decimal places is an error (storage is NUMERIC(18,4)). Parsing never stops at the first bad value: every cell gets its own result.
- **Dates:** date-formatted cells are dates; a bare number under a date column is an error; text `YYYY-MM-DD` is unambiguous; for `d/m/y` versus `m/d/y` the order is inferred from values above 12 or from which order fits the period, else the user chooses it once; values with a time zone offset are refused.
- **Template (S2):** `GET …/imports/template.xlsx` builds a workbook for the period: Driver number, Driver name, Date and one column per operational definition, one row per eligible driver per period day, every text written as a string (a name starting with `=` is never a formula). Its exact headers map automatically; the hidden period stamp (L5) is deferred.

### 4.1 Hard limits and enforcement (D9)

All limits are inclusive. Size labels use decimal bytes: 1 MB = 1,000,000 bytes and 1 KB = 1,000 bytes. Text length is measured as UTF-8 bytes of decoded text, not a count of characters.

| Bound | C2 v1 maximum | Enforcement point |
|---|---:|---|
| Uploaded file size | **10 MB** | Count actual received file bytes while streaming, before unbounded multipart buffering/spooling or persistence; a declared content length is not sufficient |
| Decompressed XLSX content | **50 MB** | Whole archive, across all entries; validate declared sizes before parsing and enforce actual decompressed bytes while reading |
| Sheets per workbook | **10** | Workbook inspection, including hidden sheets, before selected-sheet processing |
| Rows in the selected sheet | **20,000** | Bounded selected-sheet reading; includes the header and preceding rows |
| Columns in the selected sheet | **50** | Bounded selected-sheet reading, including unmapped columns |
| Cells in the selected sheet | **1,000,000** | Selected-sheet rectangular extent (rows × columns), including empty positions within that extent; do not count only mapped/non-empty cells |
| Text content per individual cell | **8 KB** | Bounded text decoding, including headers and ignored columns; applies to workbook/CSV text cells, not only validated quantity fields |

For CSV, the file is the one selected sheet. XLSX selected-sheet extents must be verified from actual content, not trusted worksheet dimension metadata; no silent dropping of out-of-range rows/columns. Reapply selected-sheet limits when the sheet/header interpretation changes. Whole-workbook decompression, sheet-count and per-cell text protections are not bypassed by selecting a smaller sheet. Generated templates must themselves fit these ingestion bounds; if a period's full template cannot fit, explain the limit rather than truncate it or emit an unimportable template.

An over-limit or unsafe upload stops inspection/staging and returns an actionable rejection naming the exceeded bound and supported maximum (without leaking another tenant's data). Do not retain a partially accepted file/import on failure; release invocation-owned temporary resources. An invalid reinterpretation leaves the last accepted staging state unchanged. Accepted files can still contain mapped validation errors discovered in A2; distinguish those row/cell issues from rejecting an unsafe or over-limit file. A3 renders these failures and allows correction/re-upload, not a bypass or partial-file import.

### 4.2 Interpretation contract (D12)

A1 inspection and interpretation responses expose the selected sheet, selected/detected header row and values, detected CSV encoding/delimiter, and date-order state (selected, detected or still unresolved). The user can select a sheet/header and, when relevant, a date order before relying on validation results. Before `WorkDate` is mapped, do not invent a date interpretation: report that mapping is required. A2 owns date inference/validation against the mapped date column and period under the rules above; every preview carries the interpretation used. A3 displays this metadata with the results, including an unresolved/ambiguous state rather than a guessed date order. Changing interpretation invalidates dependent mapping/preview results and requires recomputation.

Treat driver numbers, Employee keys and other identifier text as text where practical, preserving leading zeros. Do not invent padding or recover information Excel has already discarded; retain the existing numeric-identifier warning. Show that XLSX formula results are cached values and are not recalculated. These are file/column interpretation choices; they are not permission to confirm a Driver match or resolve a payroll conflict in Slice A.

## 5. Validation (D3)

- An **error** is incoming data whose destination — driver, date, pay item, target column or any other required destination — Flussra cannot determine with confidence.
- A **decision** is understood data that cannot be accepted without the user's choice: a driver proposed by name, a conflict with an existing value (D1, D2), work on a full-day-off Status day (§5.4), work on a normal day off (§5.5).
- A **warning** is understood data that looks unusual and needs no choice.

Errors and open decisions prevent the commit. The user resolves each one or deliberately excludes the affected data and continues with the rest. Warnings never block.

The resolution actions described in §§5.2, 5.4 and 5.5 are Slice B behavior. A2 discovers, classifies and explains these cases; A3 displays them as unresolved. Slice A does not submit decisions, confirm matches, accept exceptions or exclude rows/cells to resolve a case.

### 5.1 File and mapping
Unreadable or unsafe file, limits exceeded, no header, missing required targets, two columns on one target, a non-empty column left unmapped.

### 5.2 Drivers
- Each distinct identifier becomes one driver match, resolved to a permanent Driver among the drivers on the period's roster: by `DriverNumber`, then `EmployeeKey`, then an exact normalized full name held by exactly one roster Driver (counted by Driver, not by assignment). Messages never reveal drivers or branches outside the period.
- A name match is **proposed** and needs confirmation (S5, R13); one confirmation covers every row with that identifier, and proposals can be confirmed together.
- No match, or a name shared by several Drivers → error; the user may choose the Driver for that identifier (`UserChosen`).
- Several identifier columns mapped and disagreeing on a row → error.
- A numeric identifier that may have lost leading zeros → warning.
- Per row, the DriverAssignment is the Driver's assignment eligible on the row's date, by the exact Day Grid rules: snapshot window plus live lifecycle for a new value; existing-source rescue allowed for changing or clearing a value. No eligible assignment on that date → error ("not in this payroll on …").

**DWR-023 ownership:** A2 owns the C2 v1 identity resolver and the per-import `PayrollWorkDriverMatches` record. Read [DWR-023](FLUSSRA_DEFERRED_WORK_REGISTER_EN.md#dwr-023--external-driver-identifiers-and-aliases-are-deferred-to-c2-import) together with product-plan L3: resolve the file's identifiers through the existing company-scoped `DriverNumber`/`EmployeeKey` and permanent Driver, then the effective DriverAssignment for the Branch/date. Names produce proposals, not confirmations. An unknown source-system identifier remains unresolved; do not invent a master-data identifier, guess a Driver or create an external alias. Slice B owns user choices and confirmations within this import. Remembering source-system + external-key → Driver aliases across imports remains the later L3 capability; A1–A3 do not build or close that capability.

DWR-023's `import.ImportRows.MatchedDriverID` current-state reference describes the dormant pre-`0094` schema, not the new resolver's authority. A1 retires that schema under §3.1 and updates the register's current-state reference when the retirement actually lands; A2 uses the replacement model above. Do not resurrect `ExternalDriverID` or `core.DriverAliases`, and do not label DWR-023 implemented merely because its design responsibility has been assigned.

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
- **Row and import fingerprints (A2):** `RowFingerprint` identifies a canonical mapped source row with its identifiers, date, quantities and interpretation/mapping context. `MappingFingerprint` identifies that context; `ContentFingerprint` identifies canonical mapped import content (F6), not a browser page. Define stable ordering, identifier/text handling, exact Decimal representation and blank/zero distinctions; generated database ids and timestamps must not make identical content unstable. The A1 file SHA-256 is a separate byte-level digest. A2 records and verifies these canonicalization rules.
- **Plan fingerprint:** SHA-256 over the whole preview's target cells — target, incoming value, `ExpectedCurrentQuantity`, held full-day-off Status, outcome and every decision — plus driver confirmations and the interpretation/mapping context. A2 produces it with decisions/confirmations unresolved; Slice B extends it with the user's decisions and exclusions. Returned with the preview, sent with the Slice B commit. Pagination/filtering never changes fingerprint scope. Mapping, interpretation, source or relevant current-payroll changes require preview recomputation.
- **Refresh:** an explicit refresh re-runs resolution, validation and the diff against current payroll.
- **Commit-time revalidation (S10):** under the locks the commit re-runs the same resolver, validator and diff and recomputes the plan fingerprint. A difference refuses the commit with 409 `IMPORT_PLAN_STALE`, lists the changed cells, and writes nothing; the import stays in review.
- **Same file again (S11):** an upload whose `FileSha256` or `ContentFingerprint` equals a `Completed` import of the same period is flagged ("already imported at … by …"). A2 still compares current facts rather than assuming they are unchanged because a hash matches. Slice B owns the S11 no-reapply behavior; this flag does not authorize overwriting later manual changes.

### 6.1 Bounded preview API (D11)

The import detail response carries bounded metadata, interpretation, whole-import summary counts and page information/links. It must not embed an entire workbook or unbounded staged rows/issues. A2 supplies paginated preview rows and paginated issues, with filters for errors, warnings and conflicts. Results retain source row/column positions, incoming and current values where applicable, and the reason for each issue or unresolved match. Whole-import counts remain distinguishable from filtered/page counts. Unresolved decisions are visible, not executable actions.

A2 chooses concrete page parameters, default sizes, hard maximum sizes and serialized-response bounds using existing API conventions and measured behavior. Record those numeric bounds in the API contract and verification before A2 is accepted; omitted or excessive page-size values cannot create an unlimited response. Use deterministic ordering and associate every page with the same preview fingerprint/version; a refresh must not silently combine old and new pages. Count and fingerprint the complete import on the server, not just the displayed subset. Verify that traversing pages/filter results loses or duplicates no rows/issues, and that every page is company/Branch scoped. No processing-status polling API is added.

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

| Method and path | Permission | Primary delivery owner and purpose |
|---|---|---|
| `POST /payroll/periods/{period_id}/imports` (multipart file) | `payroll.entry` | A1: bounded file inspection/staging, sheet list and header suggestion. A2 adds column-map suggestions; no payroll write |
| `GET /payroll/periods/{period_id}/imports/template.xlsx` | `payroll.view` or `payroll.entry` | A2: the period's bounded template |
| `GET /payroll/periods/{period_id}/imports` | `payroll.view` or `payroll.entry` | Slice B: period import-history presentation; A3 does not implement history |
| `GET /payroll/imports/{import_id}` | `payroll.view` or `payroll.entry` | A1: bounded staging/interpretation metadata; A2: bounded preview pages and issue access (§6.1); Slice B: receipt |
| `PUT /payroll/imports/{import_id}/mapping` | `payroll.entry` | A1: interpretation-only fields (sheet, header, date-order selection). A2 extends it with column mapping and preview computation |
| `PUT /payroll/imports/{import_id}/decisions` | `payroll.entry` | Slice B only: Driver confirmations/choices, conflict decisions, Status/day-off decisions and exclusions |
| `POST /payroll/imports/{import_id}/refresh` | `payroll.entry` | A2: re-run preview against current payroll; no decisions or writes to payroll |
| `POST /payroll/imports/{import_id}/commit` (body: plan fingerprint) | `payroll.entry` | Slice B only: commit (§7) |
| `POST /payroll/imports/{import_id}/cancel` | `payroll.entry` | A1: cancel the whole staged import; never resolve a row/cell decision or apply payroll |

Every route resolves the import's period through `get_period_by_id` (Company and branch scope) and is listed in `route_inventory.py`; Driver/Self subjects are refused. An import whose period is no longer source-editable is shown as no longer committable.

The shared mapping resource has an explicit staged contract: A1 accepts and persists interpretation fields only, and reports unsupported column-mapping/decision input rather than ignoring it. It does not depend on A2 validation to inspect a file. A2 adds the mapping/preview portion after A1 review. Inspection metadata and samples are bounded in A1; A2 fixes the detailed preview/issue paging contract in §6.1. Neither mission exposes Slice B decision/commit endpoints or makes a batch `Completed`. Cancellation is whole-batch lifecycle behavior, not a shortcut for excluding problematic rows.

A1's JSON interpretation body uses `expected_revision`, optional `sheet_name`, optional `header_row_number` and optional `date_order` (`DMY`/`MDY`/`YMD`, or null for unresolved order). Omitted interpretation fields retain their accepted values; explicit null re-runs detection/clears order selection. ISO dates remain unambiguous without an explicit `YMD` selection; date-column inference and business validation belong to A2. Cancel accepts `expected_revision` only. Unknown fields are refused. Upload requires the `Idempotency-Key` UUID header (§3.2.1); all reads include the current `revision`, contract versions, durable File metadata and interpretation (including CSV encoding/delimiter), never File bytes or staged row dumps. After finalization, the same accepted interpretation remains readable with null inspection data. Upload/interpretation overload returns 429 `IMPORT_BUSY` with `Retry-After: 1`; a parser failure is an actionable 422 and creates no partial Import.

## 9. Frontend
Under `frontend/src/pages/payroll/entry/import/`. **A3 connects only Upload → Mapping → Preview**, using A1/A2 APIs: file selection/template download; selected sheet/header/date interpretation; column mapping; bounded preview rows, summaries and issue filters; refresh, recoverable request errors and correction/re-upload. Do not render a successful upload as an applied import, or offer nonfunctional confirm/overwrite/exclude/apply controls. Display unresolved name proposals, conflicts, full-day-off Status cases and normal-day-off cases with their reasons; their decision actions remain unavailable in Slice A. No payroll value or Status is changed.

**Slice B completes the existing v1 flow:** Driver confirmations/choices; per-cell/bulk conflict decisions; the full-day-off Status list of affected drivers once each, none selected, with select all; day-off acceptance/exclusion; row/cell exclusions; impact summary with "Import N changes" and Status-removal count; atomic commit; receipt with Day Grid/payroll-preview links; and period import history. Keeping these controls downstream does not remove them from v1.

## 10. Build order and mission gates (D8, D13)

The Day Grid workspace and the C2 product/design documents are prerequisites. **Slice A remains one feature milestone, implemented as three sequential, independently reviewable missions: A1 → A2 → A3.** Finish the scope, verification and review of A1 before beginning A2; finish and review A2 before beginning A3. Slice B begins only after A3 completes and is reviewed. Landing a schema or route does not authorize a later mission's behavior or public upload exposure before the retention gate.

### 10.1 A1 — Safe upload and staging

**Dependencies:** the merged Day Grid/whole-period-read baseline; §§2–4 and §8 of this design; existing Company/Branch/period authorization and finalization lifecycle. A1 must not depend on an A2 mapper, A3 UI or Slice B apply function to meet its gate.

**Scope:**
- Migration `0094`: the fail-closed dormant-schema retirement and new staging schema in §3, together with the named single-head/schema-authority/seed-reset compatibility updates in §3.1.
- Synchronous CSV/XLSX ingestion, actual-content validation, all seven hard limits and their failure behavior (§4.1), safe XML/archive handling and resource cleanup.
- Original-file staging, file digest, bounded inspection metadata, sheet selection, header detection/selection, and the interpretation-only API portion in §8. Preserve text identifiers and formula/cache distinctions for A2; expose interpretation state as specified in §4.2 without attempting payroll mapping.
- Required authentication, Company/Branch/period access, non-driver boundary, whole-batch cancellation and non-editable-period handling. Register every introduced route in the authorization inventory.
- Finalization cleanup for all staged-import states, the precise immutability exception and protection against upload/refresh racing finalization (§3.3). A1 owns this gate before upload exposure; no Slice B apply behavior is needed for cleanup.
- The bounded representative/near-upper-workload synchronous benchmark and evidence in §2.1.

**Non-goals:** column mapping and business preview computation; Driver matching or user confirmation; conflict/exception/exclusion decisions; payroll writes, calculation, SourceType mutation, commit, receipt/history UI; saved external aliases; queues or background import jobs or polling/status APIs; the connected UI (A3).

**Completion/acceptance gate:** files can be safely uploaded, inspected, interpreted and staged without changing payroll or any canonical payroll facts. Each hard limit has an enforcement point and actionable failure. Failed ingestion leaves no partial file/import; failed reinterpretation preserves prior accepted state. The original file is retained only under D7. Finalization purges its detail even if it never advanced beyond `NeedsMapping`, without fabricating completion or affecting payroll outcomes. Benchmark evidence supports the synchronous limits; unsuitable evidence stops acceptance pending an explicit design amendment.

**Verification:** focused parser tests for accepted/refused content, malformed archives/XML, all caps at and beyond the boundary, workbook versus selected-sheet scope, UTF-8 text-byte limits, misleading dimensions, hidden sheets, encodings, header changes, text identifiers and formulas with/without caches. API/database checks prove tenant isolation, non-driver refusal, allowed staging/cancel transitions, atomic rejection, zero canonical-payroll mutations, required cleanup and its rollback behavior, and serialization with finalization. Verify a fresh `0094` chain, current schema constraints, canonical seed/reset and SQL/Alembic equivalence. Record measured benchmark inputs/environment/timing/resources and the review result. Verify inspection responses/samples are bounded. A1 is handed off with these contracts and evidence, not a partially functioning decision/apply path.

### 10.2 A2 — Mapping and accurate preview

**Dependencies:** A1 completed and reviewed, including its schema, bounded reader, interpretation API, access and retention contracts. Reuse those contracts; do not create a second importer or identifier authority.

**Scope:** exact automatic/manual column mapping and explicit Ignore; DWR-023's current-design per-import resolution (§5.2); date interpretation, typed/domain validation, eligibility and duplicate detection; stable mapped row/import/context fingerprints; set-based current-value reads and comparison (§6); preview refresh and completed-file duplicate notices; bounded template generation; the paginated rows/issues, summary counts and error/warning/conflict filters in §6.1. Expose invalid rows, unresolved identities, warnings, conflicts, incoming values and current values accurately. Detecting an outcome is not accepting a decision.

**Non-goals:** user-chosen Driver matches or confirmations; keep/replace choices, exception acceptance, row/cell exclusions and other resolution actions; payroll mutation/commit, evidence application, SourceType changes, receipt/history; saved source-system aliases across imports; connected UI (A3); asynchronous processing.

**Completion/acceptance gate:** preview accurately represents the payroll facts that would be affected, including invalid rows, unresolved identities, warnings, conflicts and current-value comparisons. Every result carries the interpretation and preview identity used. Summary, filtering and paging agree with the complete import; no page/filter changes the hash scope or silently drops issues. Numeric default/max page sizes and response bounds are chosen, measured, documented and enforced before acceptance. Refresh handles relevant live changes without applying or deciding anything. Template output obeys the ingestion contract. The extended bounded workload remains suitable for synchronous requests.

**Verification:** database-free cases for mapping/normalization, Decimal precision, blank versus zero, date ambiguity, missing required formula caches, in-file duplicates and deterministic fingerprints. Focused integration cases cover stable identifiers, same-name proposals, disagreeing identifiers, unresolved external values, multiple assignments of one Driver, lifecycle eligibility/rescue, no cross-tenant leakage, existing-zero/current-value comparison, stale refresh, generated templates and completed-file notices. Traverse all pages and filters against expected whole-import counts; exercise absent/excessive page sizes, maximum responses and refresh between pages. Prove no user decision, confirmation, canonical fact or payroll evidence is written. Recheck scoped access and finalization-purge behavior on A2 rows/issues/matches and record the extended benchmark, including bulk-staging and purge costs of per-detail-row database guards and repeated parent lookups/locks. Preserve the guards until a measured correctness/scaling problem warrants an explicit change.

### 10.3 A3 — Connected user flow

**Dependencies:** A2 completed and reviewed; stable A1 inspection and A2 bounded-preview contracts; A1 retention cleanup is working before upload becomes an available product workflow.

**Scope:** connect the period entry page to **Upload → Mapping → Preview** (§9), including template download, selected sheet/header/date interpretation, useful request/validation errors, paginated rows/issues and error/warning/conflict filters. Display source coordinates, incoming/current values, unresolved identities and decision reasons. Provide correction/re-upload, supported whole-import cancel, interpretation/mapping correction and synchronous refresh/retry states. During a pending request, show request-local progress/loading; never poll a background job. Failed responses cannot be shown as successful staging or an applied import; after an uncertain response, use available import metadata or correction/re-upload rather than assume a commit occurred.

**Non-goals:** deciding any match, conflict, replacement or exception; row/cell exclusion to resolve a case; confirm/apply/commit controls; payroll mutation, receipt/history; asynchronous status workflows or inline source-value editing. Keep the existing complete v1 decision flow for Slice B, rather than adding dead controls to Slice A.

**Completion/acceptance gate:** an authorized user can go from file selection through a reliable preview, see the interpretation used and inspect all relevant outcomes through bounded pages/filters, recover from supported failures, and refresh after changes. The workflow cannot decide or apply payroll changes. Upload exposure is blocked until retention/access gates are satisfied. Slice A completion means preview completion, not a completed payroll import.

**Verification:** connected UI/API scenarios for an accepted file, template round-trip, interpretation correction, bad/over-limit file, invalid/unresolved/conflicting rows, warnings, empty/zero outcomes, pagination/filtering, failed requests and refresh. Verify inaccessible periods and Driver/Self users cannot use the workflow. Verify finalized periods cannot recreate purged details. Prove payroll facts remain unchanged from file selection through preview/cancel. Include a representative near-bound workload so the UI neither requests nor renders the entire workbook at once.

### 10.4 Slice B — Decisions and commit, downstream

**Dependency:** all of A1, A2 and A3 completed and reviewed. Slice B alone enables user match choices/confirmations, conflict/overwrite decisions, full-day-off Status removal choices, configured-day-off exception decisions, exclusions, final impact confirmation and applying the import. It owns the quantity write path, lock-ordered atomic commit with revalidation, evidence/provenance links, SourceType rules, idempotent completion, receipt and import history. It verifies completed-import retention through the lifecycle A1 already provides. §§5.4–5.5 and §7 describe these downstream behaviors, not permission to build them in A1–A3.

### 10.5 Requirement ownership and cross-cutting contracts

Each row has one primary delivery owner. A later mission's consumption or verification is not a second implementation owner. Cross-cutting contracts remain binding in every mission that uses them.

| Existing Slice A requirement / contract | Primary owner | Consumer or verification boundary |
|---|---|---|
| `0094`, dormant retirement, target schema, schema/head/seed-reset updates (§3.1) | A1 | A2/B populate only their assigned fields |
| CSV/XLSX reading, real content checks, hard limits, synchronous parsing and failure cleanup (§§2.1, 4.1) | A1 | A2 reuses the reader; A3 displays failures |
| File staging and byte-level file fingerprint (§3.2, F6) | A1 | A2 adds mapped-content fingerprints |
| Sheet/header selection and interpretation metadata contract (§4.2, S3) | A1 | A2 adds mapped-date inference; A3 renders the same metadata |
| Authorization, tenant/Branch scoping, non-driver boundary, editable-period guards (§8) | A1 cross-cutting contract | A2 applies to every added page/route; A3 verifies connected access |
| Finalization purge and its serialization/immutability exception (D7, §3.3) | A1 cross-cutting contract | A2 adds detail coverage; A3 checks exposure; B adds completed-import verification |
| Bounded synchronous intake benchmark (§2.1) | A1 cross-cutting contract | A2 measures added preview work; A3 checks connected workload |
| Column mapping, exact suggestions, Ignore and mapped row storage (S4, F4) | A2 | A3 supplies mapping controls, not payroll decisions |
| Driver/external identifier resolution required by v1, per-import matches (S5, DWR-023, L3) | A2 | A3 displays unresolved proposals; B owns choices/confirmations; saved aliases stay deferred |
| Mapped date inference, type/unit/date/eligibility checks, duplicates and skipped rows (S6, S8, §5) | A2 | A1 preserves parser distinctions; A3 displays results |
| Set-based current-value read, blank/zero and conflict comparison (§6, S7) | A2 | A3 displays; B resolves conflicts |
| Row, mapping, content and whole-preview fingerprints (§6, F6) | A2 | A3 preserves preview identity; B revalidates before commit |
| Refresh and completed-file notices (S10/S11 preview portions) | A2 | A3 refreshes/displays; B owns commit-time revalidation and no-reapply behavior |
| Period template (S2), bounded preview rows/issues, summaries and filters (S8/S9 preview portions) | A2 | A3 consumes bounded responses |
| Upload → Mapping → Preview screens, interpretation display, error/recovery states and end-to-end flow (§9) | A3 | Uses A1/A2 contracts without duplicating their rules |
| Choices, confirmations, resolution/exclusion actions, impact confirmation and all apply behavior | Slice B | Excluded from every Slice A mission |

## 11. Tests (behavior-named)

The mission-specific checks in §10 are the acceptance evidence for A1–A3. Use focused parser/unit, schema/API, authorization and connected-workflow checks appropriate to each change, with behavior-named tests.

Slice B additionally verifies choosing/confirming matches; the full-day-off list once per Driver, nothing selected by default; selecting removes exactly the chosen Statuses, preserves notes and accepts the work; exclusions do not apply data; configured-day-off answers accept with a notice, exclude, or block when unanswered; value/held-Status decision invalidation; commit-time stale refusal with no writes; source-editable-state refusal; exactly decided changed quantities, idempotent repeat and lock order; permanent correlation/evidence and SourceType behavior; finalization purge of completed imports with durable record/summary preserved. Day Grid/public line route regressions include the forced `Manual` behavior only when Slice B implements it.

## 12. Deliberate deviations from the plan
There are no differing Slice A instructions between this design and the product plan. Both use synchronous requests and persisted `NeedsMapping`, `NeedsReview`, `Completed` and `Cancelled` states; readiness is computed, not a background-job state. Slice A never completes an import. Both defer the template's hidden period stamp under L5; exact headers already map the template. Slice A mission completion does not mark a C2 product capability or DWR-023 implemented before its code and verification exist.

## 13. Decisions
The original implementation decisions D4–D7 are recorded in `FLUSSRA_C2_IMPORT_PLAN.md` §11 (decided 2026-10-24):
- **D4** — version 1 reads one row per driver per day; other layouts come later as reader modes (§4, §3.2).
- **D5** — work on a full-day-off Status day is resolved inside Review, per affected driver (§5.4).
- **D6** — work on a normal day off is accepted as a reviewed exception or excluded; unanswered blocks (§5.5).
- **D7** — detailed provenance and the original file are kept until finalization, then purged; the durable import record stays (§3.3).

The 2026-10-25 delivery/operational decisions are recorded as D8–D20 in the product plan §11: sequential A1/A2/A3 delivery (§10); hard limits and actual-content checks (§4.1); synchronous processing with measured evidence (§2.1); bounded preview pagination (§6.1); visible spreadsheet interpretation/cached-value semantics (§4.2); Slice B decision/apply boundaries; neutral core and specialized source/target ownership; revision/idempotency/version contracts; and Import-owned target retention (§§2–3, 9–10).
