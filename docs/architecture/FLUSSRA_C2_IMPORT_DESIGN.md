# FLUSSRA C2 IMPORT DESIGN — PAYROLL WORK DATA

**Authority:** Agreed refoundation contract (2026-10-26), implementing the [C2 product plan](FLUSSRA_C2_IMPORT_PLAN.md). It supersedes the earlier single-file/selected-sheet/global-mapping and relational-cell preview design. Main's A1 foundation is merged at `337e3f7` (PR #74, schema `0094`). Unmerged `6498453` is implementation/measurement evidence only, not a migration or architectural authority. This backend refoundation and bulk-write feasibility gate precede A3. A3 UI and public Slice B decisions/apply remain downstream.

## 1. Domain boundaries

A Payroll Period may receive many Imports while editable. One PayrollWork Import is one user-submitted batch, owned by exactly one Company, Branch and Payroll Period; it need not complete that period or supply every frozen Pay Definition.

```
Import batch
  -> immutable source files
  -> CSV / selected worksheet source units and accepted interpretation
  -> source-layout groups with separate accepted mappings
  -> compact source/normalized records and scoped identity matches
  -> combined Driver + WorkDate impact grid, issues and source drilldown
  -> future reviewed canonical bulk quantity mutation
```

Canonical `PayrollDraftLines` remain ordinary source facts at DriverAssignment + WorkDate + PayrollPeriodDefinition + Quantity. Money remains the calculation engine's responsibility. Import neither creates master data nor writes payroll/evidence during upload, interpretation, mapping or preview. No Status, Bonus, People, Rates or Access import is included.

The immutable period definition layout is the target authority. No Pay Definition name, code, source label or assumed count is a production constant. The fixture dimensions 150 Drivers, 31 days, 20 definitions, 10,000 source rows and approximately 200,000 candidates are measurements, never domain limits.

## 2. Sources, layouts and accepted meaning

### 2.1 Files and source units

One batch accepts multiple CSV/XLSX files. CSV contributes one source unit; XLSX exposes each worksheet as a source unit and allows multiple selections. Workbook/source-unit counts are operational policy, not schema limits. At least 31 daily files and a workbook with 31 daily worksheets must pass verification. Hidden worksheets require explicit selection; chart sheets are not data worksheets.

Files have immutable batch ordinal, original filename, type, size, byte digest and bytes until retention. Source-unit identity includes its owning file, physical unit name and unit ordinal. Repeated sheet names or row numbers in different files cannot collide. Repeated filenames are allowed; the visible batch file ordinal disambiguates their physical provenance.

CSV's single unit is initially selected. Workbook units are inspected and initially unselected; selection is accepted explicitly. Every accepted mapping read exposes its current source-unit membership before the user relies on it.

### 2.2 Layout authority

A SourceLayout belongs to one Import, not a physical file or the whole period. Compatible units may share it across CSV files and worksheets. Deterministic grouping uses relevant ordered header structure/column positions, not sample payroll values, filenames, Pay Definition counts or AI. Duplicate header labels retain their positions. Materially different structures get separate layout groups.

Grouping is a reviewable suggestion. An explicit split/regroup operation can assign compatible units to a separate layout when the same physical headers have different business meanings. It cannot silently copy an accepted mapping onto incompatible units. Mapping inspection returns layout ID, structure digest, mapped columns and exact file/unit membership. Definition targets come dynamically from operational frozen period definitions.

Accepted column mapping is one-based source column -> DriverNumber, EmployeeKey, DriverName, WorkDate, Definition:<period-definition-reference>, or Ignore. Require at least one identity role and one quantity target; prohibit duplicate target roles/definitions. Every non-empty column must be mapped or explicitly ignored. WorkDate is required after normalization, but its source column is optional under the accepted context contract below.

Header/selection changes regroup affected units and invalidate preview. A mapping can be reused only when its source structure remains compatible; changed structures require acceptance again. Unused layout metadata is not evidence that a unit is selected.

### 2.3 WorkDate providers

Each selected source unit uses one effective date provider:

- an explicitly mapped WorkDate column;
- accepted filename date context;
- accepted worksheet-name date context;
- an explicit fixed source-unit date;
- an explicitly accepted Import-level fixed date.

A mapped WorkDate role is explicit acceptance of the column provider. Context providers require their kind and accepted ISO date to be submitted explicitly. Filename/sheet detection returns candidate dates and ambiguity; it never supplies authoritative context by itself. An accepted filename/sheet date must match a detected candidate; a user wanting another date must select fixed context. A context provider and mapped date column cannot compete silently: reject that conflicting configuration.

Numeric/slash date-order inference for column values follows the existing unambiguous/selected-order contract. Ambiguous values require accepted date order. Persist provider kind/value, header, date order and CSV decoding interpretation as provenance. The preview reports both provider and interpretation. A validly interpreted date outside the target period/day snapshot is a validation error, not a request to route to another period.

## 3. Supplied facts, identity and comparison

### 3.1 Coverage

Only mapped definition columns belong to a source's supplied set. Omitted/unmapped definitions create no candidate, blank marker, zero, clear operation or unchanged evidence. Later partial Imports compare only their supplied keys against then-current payroll; other definitions and notes remain untouched.

For a mapped definition, blank means no incoming quantity and zero is explicit zero. Preserve that distinction in normalized data and fingerprints. All-blank source rows are skipped without Driver/date errors or confirmation requirements for nonexistent incoming facts; a missing required formula cache is not a blank. Count mapped candidates, including blanks/invalid values, for admission resource accounting. XLSX numeric values and numeric cached formula results retain their XML numeric lexemes for exact Decimal validation, rather than first rounding through a binary float; workbook-styled dates/times/durations keep their interpreted types.

Typed values use exact finite Decimals and canonical quantity validation, including frozen InputType and supported calculation method/version. Flussra never evaluates formulas: use cached results; missing required caches are errors. Identifier text is preserved where practical; numeric identifiers warn without invented leading-zero padding.

### 3.2 Permanent Driver before eligibility

Resolve DriverNumber and EmployeeKey in one set-based, Company-scoped permanent Driver/Employee read, independently of period roster membership. All supplied stable keys must agree. Cross-Company identities remain undisclosed. Exact unique normalized roster names are proposals only, not confirmations.

Then resolve the target Branch/WorkDate assignment using the canonical period snapshot plus live lifecycle date matrix. A known Company Driver without a valid assignment/date stays known and receives an assignment/eligibility error. An unknown/ambiguous name does not erase agreed stable identity, though its error remains. No external alias is invented or saved.

Existing-source rescue is exact-date and permits changing an existing source, not adding another definition's new source. Preserve the difference between actual current-source presence and effective nonzero quantity; an existing zero line may matter to rescue even when comparison treats it as empty.

### 3.3 Diff and duplicates

Compare incoming supplied quantities to current ordinary, non-void payroll facts. Preserve New, Unchanged, Conflict, Blank, Invalid and Unresolved meaning. Positive work overlapping a held full-day-off Status or a configured day off exposes required decisions, without selecting replacements, removing Statuses, accepting exceptions or excluding data.

Multiple nonblank source values resolving to one DriverAssignment + WorkDate + frozen definition within the same Import are blocking duplicates, including across files, units and layouts. Do not sum or pick a winner. Blank supplies no quantity. Invalid inputs remain traceable and block readiness independently.

Separate Imports are allowed to overlap: the later one compares against current facts and exposes ordinary conflicts. A completed-import notice is informational/idempotent protection, not a prohibition on partial later batches.

## 4. Compact schema and cutover

### 4.1 Migration authority

Main owns `0094`; corrected forward migration `0095` is based directly on it. Do not copy unmerged A2's lookup-index migration or rewrite `0094`.

Clean cutover removes disposable A1 PayrollWork staging rather than migrating obsolete per-cell previews. Refuse unsupported non-PayrollWork staging or apparent applied/completed evidence rather than deleting canonical lineage. No canonical payroll/audit/evidence table is reset. Historical `0094` retirement tests remain historical; current-head checks advance separately. Fresh SQL and Alembic authorities must agree.

### 4.2 Ownership model

All specialized ownership is Company/Import scoped with composite FKs. Generic Import/source/issue contracts do not require a Driver, worksheet, date or payroll target.

| Table | Role |
|---|---|
| Imports | Existing neutral envelope, lifecycle, revision, correlation, idempotency and actors |
| FileSourceBatches | File-source contract/version and immutable ordered creation manifest; source metadata is not placed on the PayrollWork target |
| FileSources | Many immutable files per Import, unique batch file ordinal; content/inspection purge markers |
| FileSourceUnits | File-owned CSV/worksheet identity; selection, header, date provider/order and current layout binding |
| SourceLayouts | Import-owned structure digest and accepted mapping; transient header/inspection samples kept separate |
| ImportSourceRecords | Neutral source locator and bounded mapped typed-source payload, one per source row |
| FileSourceRecords | Source-record -> source unit + physical row; uniqueness is unit + row, not Import + row |
| PayrollWorkImports | Period ownership, accepted Import fixed-date context, versions, mapping/content/plan fingerprints, counts and future receipt summary; no global ColumnMapping |
| PayrollWorkRecords | One compact normalized source record: Driver/assignment/date scope, preview-group reference, RowFingerprint, skipped state, supplied-values JSON and issues JSON |
| PayrollWorkDriverMatches | One scoped identifier match/proposal; later confirmations remain Slice B |
| PayrollWorkRecordIdentifiers | Source identifier-column associations with their match, never one row per quantity |
| PayrollWorkPreviewGroups | One Driver + WorkDate grouping (or distinct unresolved/skipped source grouping), severity counts and ownership, without copying every value again |
| ImportIssues | Neutral batch-level issues where relational issue identity is useful; cell issues remain compact record detail |

There is **no PayrollWorkCells table** in the corrected authority. No fixed Pay Definition columns are added. Sparse future decisions and durable applied-change/evidence links belong to Slice B and do not require materializing every candidate now.

Normalized values are keyed dynamically by period definition reference with source column, incoming exact quantity/blank state, current source presence/value and outcome. Shared Driver/date/Status/day context is record-level. Source raw values remain in the neutral source payload and are projected for issue/source reads. Guarded relational rows name owners, matches, groups and normalized records; transient per-value data is structured payload.

Database guards repeat Company/Branch/period ownership, immutable file/target identity, payload shape and same-period definition membership, source editability and retention rules. Moving definition references into JSON does not remove ownership validation. Operational capacities are application configuration, not schema maxima.

## 5. Reusable processing and atomic publication

HTTP routes authorize/decode DTOs and invoke application services. File parsing, source-layout interpretation, normalization/validation, current-value comparison and prepared publication are reusable boundaries with no Request dependency. A future durable worker can call them without rewriting the Import domain.

1. Authorize Company/Branch/period and non-driver actor in a short read transaction.
2. Receive/inspect the complete manifest under aggregate admission; no retained connection or period lock during parsing.
3. Load required bytes/configuration/revision, close the connection, parse selected units once per physical file. XLSX archive safety scanning is once per file per parse, not repeated once per selected worksheet.
4. Read lean canonical context set-wise; normalize/compare/hash off-thread. No N+1 identity or payroll reads and no Day Grid screen/pay calculation.
5. Prepare bounded streams/batches outside the write transaction; avoid a second full candidate matrix.
6. Reauthorize, lock period before Import, recheck revision/editability and complete relational context token.
7. Replace derived detail and publish all counts/fingerprints atomically in one transaction. Readers see complete old or complete new state. Failures roll back and close all invocation-owned temporary resources.

Source/configuration mutations require expected_revision, increment once and invalidate preview atomically. Stale preparation cannot overwrite newer selection, grouping, mapping, payroll context or finalization. Admission rejection is retryable, never an unbounded in-memory processing queue.

## 6. Semantic identities

The generic envelope stays version 1; File batch/file source and PayrollWork target contracts are version **2**, with canonicalization version **3**. Unsupported stored versions fail closed, with reset/re-upload rather than adapters.

SHA-256 identities use deterministic versioned canonical UTF-8 JSON, exact Decimal normalization, blank distinct from zero and sorted multisets retaining duplicates. Generated Import/file/unit/layout/source/match/group/period/definition/Driver/assignment IDs and audit timestamps never enter public semantic hashes. Relational IDs remain legitimate ownership/application references.

- MappingFingerprint: period business meaning and complete accepted unit/layout/provider contracts. Translate definition references into frozen code/name, InputType/unit, calculation method/version, rate shape and active meaning. Include actual layout assignment/source interpretation, not generated layout IDs.
- RowFingerprint: a precomputed digest of that unit's accepted semantic mapping/interpretation contract, normalized source identifiers/date and only supplied definitions/values. Invalid mapped date/quantity values retain their relevant raw/type/cache meaning. Ignored raw content cannot change semantic content.
- ContentFingerprint: accepted mapping semantics and the complete sorted row multiset. It is separate from immutable byte digests.
- PlanFingerprint: content plus permanent Driver keys, assignment business window/Branch/timezone meaning, relevant current source presence/quantities/Status/day state, outcomes, issues, source provenance and explicit unresolved decision/confirmation state.

File batch ordinal, worksheet name and physical row/column are source coordinates, not database identities. They disambiguate provenance, including repeated filenames. Byte hashes bind creation/retry identity; they must not leak ignored raw content into semantic content identity. A separate unpublished relational context token rejects stale FK references during preparation/publication.

## 7. Human preview and APIs

### 7.1 Payroll grid

The combined preview is Driver + WorkDate oriented across all selected layouts. Columns are the dynamic union of mapped frozen definitions, with code/name/unit/type from that period. Row values omit unsupplied definitions, explicitly mark mapped blanks and preserve zero. Layout mapping inspection remains separate and lists its source units.

One grid value can cite contributing source records/columns. Every supplied definition appears even when more than five layout groups contribute to one Driver/day. Each value exposes exact candidate and nonblank candidate counts, with at most five deterministic provenance samples, nonblank candidates first. Source-record samples are separately bounded to five, with exact totals and issue counts. Duplicate nonblank candidates display a blocking summary/count, never an arbitrarily chosen incoming value or implicit sum. Bounded source/issue drilldown exposes every retained candidate and physical locator. Internal granular fact iteration is not a public row-per-fact screen contract.

### 7.2 Backend surface

- POST /payroll/periods/{period_id}/imports: ordered multipart batch of one or more repeated file parts; Idempotency-Key required.
- GET /payroll/imports/{id}: bounded metadata, files/units/layout membership, accepted interpretation/mappings, revision, summaries and preview identity; never file bytes. Mapping targets use target_offset/target_limit (default 50/max 100) and mapping_target_page. Full header labels use header_layout_id/header_offset/header_limit (default/max 25) and header_page. Layout summaries show at most 12 labels of 80 characters, with exact header_count and an explicit truncation flag; they are not the full mapping-inspection contract.
- PUT /payroll/imports/{id}/interpretation: revisioned selection/header/date-provider changes, including accepted Import fixed date.
- POST /payroll/imports/{id}/layouts: explicit compatible-unit split/regroup.
- PUT /payroll/imports/{id}/layouts/{layout_id}/mapping: accept that layout's mapping, invalidate preview.
- POST /payroll/imports/{id}/preview: build/rebuild read-only combined preview from current accepted source configuration, revision protected.
- GET /payroll/imports/{id}/rows and /issues: revision/PlanFingerprint-bound paginated grid and issues.
- GET /payroll/imports/{id}/source-records: paginated source/normalized record provenance; large raw source fields use bounded cell projection/drilldown.
- GET /payroll/periods/{period_id}/imports/template.xlsx: dynamic frozen-definition template, optionally a selected definition subset; no fixed domain columns.
- POST /payroll/imports/{id}/cancel: whole uncompleted batch only.

Current page policy: grid default 25/max 100; issues default 50/max 200; source records default 25/max 100, with cell_offset/cell_limit (default 32/max 50). Serialized responses, including root metadata, are at most 2,000,000 UTF-8 bytes; a short page exposes its next offset. No whole-workbook response. Severity filters cover errors/warnings/conflicts and counts cover the complete Import. Issue pages include the offending retained value. Source-cell pages prevent one large source row from requiring an unlimited response.

Mutating operations require payroll.entry and same-Company/Branch access, non-driver actor, editable period and live Import. Reads require payroll.view or payroll.entry with the same subject/scope boundaries. Register every route in the authorization inventory. No decision, confirmation, exclusion, Status-removal, apply or processing-status endpoint is exposed.

Creation retry binds creator/target/versions plus the entire ordered manifest (filename, type, bytes/size/digest); identical key/input returns current metadata without resurrecting purged detail. Changed input conflicts. New keys may deliberately create additional partial Imports.

## 8. Configurable resource policy

The initial operating defaults below protect one submitted Import, not business cardinalities. They are provisional until the corrected workload evidence is recorded. Increasing configured capacities must not reconstruct schema or add fixed Pay Definition fields.

| Capacity | Initial default |
|---|---:|
| File bytes | 10,000,000 per file |
| Aggregate uploaded file bytes | 50,000,000 |
| Multipart overhead | 256,000 aggregate; existing bounded part headers |
| Files per batch | 64 |
| Actual decompressed XLSX bytes | 50,000,000/file; 200,000,000 across the batch |
| Worksheets/file and source units/batch | 128 each |
| Layout groups/batch | 64 |
| Selected rows/unit (including header) | 20,000 |
| Source columns/unit | 128, including identities/references/ignored fields |
| Rectangular cells/unit / selected aggregate | 1,000,000 / 2,000,000 |
| Selected data rows/batch | 50,000 |
| Mapped quantity candidates/batch | 250,000, including blanks/errors |
| Text/cell | 8,000 UTF-8 bytes |
| Inspection metadata bytes | 4,000,000, with bounded header samples |
| Concurrent heavy requests/process | 2, with immediate 429 and Retry-After |

These are configurable operational ceilings, never schema CHECK maxima for Driver/definition/file/layout counts. Aggregate byte/decompression/unit metadata checks cover the entire uploaded batch; selected-row/candidate checks cover the whole selected workload across every layout, so splitting does not bypass them. Reject atomically with actionable errors, never truncate or accept an incomplete plan.

Preserve actual-content checks, malformed/incomplete multipart refusal, filename/header bounds, ZIP path/encryption/member/CRC/actual inflated-byte checks, XML DTD/entity/depth protections, macro rejection, actual coordinates rather than forged dimensions, bounded CSV decoding and formula non-evaluation. Retain the generous 4,096 package-member and 256 XML-depth safety ceilings separately from business capacity. Workbook processing must validate the complete package before openpyxl performs member reads.

The million-cell rectangle is a parser/security ceiling, not a million relational payroll-cell acceptance target. Default capacity includes the agreed monthly and rare benchmark shapes with room for identity/reference/ignored columns. Final evidence must justify retaining or revising operational defaults without silently removing the agreed capabilities.

## 9. Retention and future bulk authority

### 9.1 Finalization

Import owns cleanup, invoked inside canonical Payroll finalization after period lock/Locked transition. Lock ordered Imports after the period. Purge every file's bytes/inspection, unit/layout inspection samples, source records/typed payloads, identifiers/matches, normalized values/issues and preview groups/plan state, including never-completed/cancelled Imports.

Retain envelope/file manifest metadata, accepted unit/provider/header interpretation, layout mapping/identity, mapping/content fingerprints and legitimate receipt/audit metadata. ReviewSummary currently retains only aggregate preview counts, not a fabricated completion receipt. Set the target DetailPurgedAtUtc marker once per Import; never depend on its file count or completion state to discover cleanup work. The narrowly scoped retention exception permits only those removals/clears; finalized periods cannot recreate detail. A failure rolls back complete finalization. All temporary preparation streams are invocation-owned and removed independently of durable Import completion.

### 9.2 Internal canonical bulk quantity boundary

Before A3, implement or conclusively measure a canonical Payroll-owned bulk boundary, not an Import-owned parallel payroll writer. It accepts explicitly reviewed supplied quantity changes; it has no HTTP apply endpoint and does not choose conflicts.

Authority scope is Company currency lock -> period mutation lock -> future caller's Import lock. Authorize/non-driver/Branch access once, validate the complete change set using frozen definitions, canonical quantity rules, period days, live/snapshot date eligibility and exact-date rescue. Reject duplicate target keys and stale expected current presence/quantities. Positive source cannot coexist with a held full-day-off Status. Notes and unsupplied definitions are untouched.

Diff under the lock; no-op quantities produce no fake mutation. Add/update/void canonical DraftLines set-wise, retaining all production constraints and triggers. Imported actual changes use Import source semantics; unchanged facts keep their provenance. No Status removal/decision is performed.

Use canonical SOURCE audit-evidence ownership and participant snapshot/coverage semantics. Emit one generic audit row and immutable SOURCE evidence event per actual change, with exact before/after target/quantity/source/status/notes and a shared correlation. Resolve relational IDs set-wise, reuse actor evidence across the batch and retain transactional rollback. Do not call the interactive mutation function thousands of times or disable production guards.

Tests must prove authorization, stale inputs, inactive/nonoperational definitions, precision/WholeNumber rules, date eligibility/rescue, off-Status rejection, unchanged/omitted facts, Import source semantics, create/update/void, audit/evidence completeness, correlation, rollback on evidence failure, and lock ordering. Measure realistic large changes with all real constraints/audit/evidence active. Bulk feasibility is a separate pre-A3 gate; no public Slice B workflow is authorized here.

## 10. Verification and acceptance

Focused verification covers fresh 0094 -> corrected 0095 cutover, disposable staging reset without canonical changes, SQL/Alembic equivalence, schema guards, canonical seed/reset, multi-file/unit/layout ownership, partial coverage, all four date providers, ambiguous context acceptance, 31-file and 31-worksheet cases, dynamic heterogeneous mappings, duplicate targets across sources, stable identity before eligibility, semantic fingerprints across surrogate IDs/layout changes, paging/bounds, stale publication races, no preview payroll writes and atomic retention.

Benchmark representative input, dense 150 Driver × 31 day × 20 definition input (93,000 potential facts), and approximately 10,000 source rows/200,000 candidates. Include heterogeneous groups, daily CSV files and daily XLSX sheets. Record parsing/normalization/preview/preparation/publication, lock/transaction duration, memory/temp resources, ordinary authenticated-read responsiveness and internal bulk canonical writes. Compare explicitly to the old guarded relational-cell publication and purge costs.

Synchronous-first remains current architecture. No authoritative request/proxy budget was found in repository configuration: the development Uvicorn launcher supplies none. Measure the corrected engine before deciding suitability. Do not invent a latency threshold. If no authoritative budget exists, state that synchronous HTTP suitability is not established. If evidence establishes the need for durable work, stop before A3 and return the smallest justified durable-worker design as a next task; do not silently build it here.

Historical unmerged A2 evidence: 939,953-cell CSV publication was about 100.75 s (68 s after COPY), refresh about 97 s publication, process peak about 1.25 GB before compact in-memory changes, and purge about 23–25 s. Guard/FK work per persisted value dominated. These explain this refoundation; they are not the new product acceptance workload.

No A3 UI or public Slice B decisions/apply are included. Final acceptance requires reviewed measurements, retained safety/canonical invariants, and a satisfactory bulk-write feasibility result.
