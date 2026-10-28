# Flussra C2 Import Plan

**Status:** Revised ordinary-work Import delivery plan, 2026-10-28. The [product contract](FLUSSRA_C2_IMPORT_CONTRACT.md) is the decision authority; the [technical design](FLUSSRA_C2_IMPORT_DESIGN.md) defines implementation boundaries. A01's configurable policy and authorized metadata boundary, A02's canonical Company/Branch capacity enforcement and retirement semantics, A03's forward lifecycle/schema, scope/version, retention and staleness foundation, A04's strict per-file canonical extraction, A05's cross-file header compatibility/combined-column accounting, A06's per-target draft mappings, A07's Company-authoritative Driver identification/discrepancy discovery, A08's chosen date authority/inference/review, A09's explicit repeated-row/category preparation, and A10's semantic repeat recognition and bounded review are implemented and focused-verified. A11's revised workload harness and shape checks are prepared; workload measurements and an authoritative deployment-budget decision remain pending. A11 is not accepted, and A12 and Slice B remain planned; payroll application, completion evidence creation and successful rule-head promotion remain later-stage work. This plan covers the complete outcome without claiming benchmark acceptance or deployment acceptance.

**Baseline:** Continue on `codex/c2-import-foundation`, preserving implementation `9e03a66` and documentation through `f15bf96`. Main's safe-upload foundation was merged at `337e3f7` (historical A1). Historical A2 preview `6498453` is reference evidence, not a branch to merge/cherry-pick. Schema `0095` was the preserved baseline; forward schema head `0097` now adds the narrow Company Driver lookup index. The compact batch backend and internal canonical quantity writer are preserved assets; they do not already satisfy the revised contract. No connected Import UI or public apply workflow is complete.

## 1. Product outcome and scope

A user imports ordinary work quantities into one editable Company/Branch payroll period. The period's frozen definitions and canonical Driver/date eligibility define the target. Money remains the calculation engine's responsibility. A period may receive many partial imports; missing coverage warns and does not imply zero, deletion or completed payroll.

One setup understands one source, potentially across compatible daily files. Another source gets its own setup afterward. Columns represent payroll inputs or shared identity/date/filter/reference fields. Calendar-looking header labels may be ordinary quantity names; headers never infer dates or create day-axis mapping. Dates come from the chosen file/sheet-name or Date Column authority. Accept `.xlsx` and `.csv`; one sheet/file, one table/sheet, first-row headers, no merged cells inside the imported table. Do not support arbitrary report layouts, multi-sheet selection or multiple independently configured source families within one setup.

V1 includes Branch-specific reusable target relationships, Direct/Sum transformations, explicit duplicate-column resolution, confirmed within-day row aggregation and categorical filtering, assisted dates, accurate bounded preview, user review and atomic application. Normal compatible repeat imports need only organized-day confirmation unless a relevant problem appears.

Detailed dedicated text-only Status import remains separate future design. People/Rate/Access imports, master-data creation, Bonus/final-pay import, a new ordinal model or tier-count cap, binary-prefix ordinal columns, additive source contributions, general spreadsheet recalculation, a company-wide source catalog, integrations, Undo and speculative background jobs are outside this delivery. Status is still one concept in the Branch capacity policy; an external category used to filter work is not Flussra Status import.

## 2. Non-negotiable rules

| ID | Revised rule |
|---|---|
| R1 | Accept a strict supported source/table contract; explain rejection and retain recoverable setup. |
| R2 | Never overwrite payroll without required review/confirmation. An identical completed business import is a no-op, preserving later manual edits. |
| R3 | Preserve value provenance until finalization, then accepted payroll facts and durable receipt/audit/rule metadata. |
| R4 | Frozen period definitions remain the target authority; do not substitute current live settings into an older period. |
| R5 | Import supplies quantities; existing payroll methods calculate money. |
| R6 | Do not create canonical Drivers, Employees, definitions, rates or Access accounts through ordinary work import. |
| R7 | Pay items are configured through their canonical governance, not imported from headers. |
| R8 | Status belongs to its own domain and dedicated future import. |
| R9 | Every preparation, mapping and preview operation is staging, with zero payroll-source writes. |
| R10 | Reviewed application is atomic; explicit exclusions are recorded and unresolved errors/decisions block it. |
| R11 | Permanent Driver identity and Branch/date eligibility are separate checks; supplied codes have authority over names. |
| R12 | Missing/blank supplies no quantity; zero is explicit zero. Missing Sum inputs skip the result with a warning. |
| R13 | Explain date interpretation; do not use numeric confidence, guess missing days or silently reinterpret changed relationships. |

## 3. Adjustable product and operational policy

| Product allowance | Initial default |
|---|---:|
| Uploaded bytes/file | 10,000,000 |
| Canonical headed-table bytes/file | 1,000,000 |
| Columns in the combined source structure | 40 |
| Currently usable Company pay items | 50 |
| Branch payroll input concepts including Status | 15 |

Defaults are centrally declared and settings-backed, not scattered production constants, database cardinality CHECKs or frontend-only constraints. Backend enforcement is authoritative; authorized policy metadata supplies displayed values and messages. No customer policy editor or entitlement framework is introduced. Raising an allowance must not require schema reconstruction or new fixed input columns.

External headers ignore capitalization and surrounding whitespace. Each pair of files must share a normalized header; a third file cannot bridge a disjoint pair. For each label, use the maximum occurrence multiplicity in any one file, then sum those maxima. Thus `Driver | Code | Hours | Hours | Hours` is five columns; another identical daily file keeps the count at five. Partial overlaps use the combined occurrence count; completely disjoint structures are rejected. Changed duplicate count/arrangement requires renewed resolution.

Hidden populated columns count and are identified. Headerless populated columns are ignored with a notice and excluded from the product column/canonical-data allowance, but remain covered by physical inspection safeguards. Canonical bytes include all headed data before mapping, including columns later set to Ignore; presentation metadata is excluded. Cached formula results and required type meaning are preserved without recalculation.

Separate configurable operational policy bounds actual files, physical columns, rows, cells, multipart/archive/XML work, decompressed bytes, response sizes, preparation resources and concurrency. Company-wide name discovery is additionally bounded by `IMPORT_MAX_IDENTITY_LOOKUP_ROWS` (100,000 Driver rows materialized for processing, plus one overflow sentinel) and streamed in `IMPORT_IDENTITY_LOOKUP_BATCH_ROWS` batches (512); a code-only import skips that scan, and an over-limit name lookup fails without publishing a partial preview. These are work limits, not Company-size product caps. Retain applicable existing safeguards provisionally; final aggregate ceilings require revised realistic measurements. Never interpret a product allowance as permission for unbounded parsing.

Retirement releases currently usable Company/Branch slots while preserving history and frozen snapshots. Enforce capacities through canonical creation/approval/activation/retirement authorities with existing locks and effective-date rules. Structural rules, mathematical validity, storage precision and contract versions remain explicit engineering contracts, not arbitrary growth knobs.

## 4. User flow and repeat behavior

1. Open the editable period; upload one source's files or use the period's dynamic template.
2. Safely extract canonical data; enforce strict table rules, per-file bytes and combined-column compatibility/capacity; disclose ignored/hidden data.
3. Reuse compatible Branch rules or prepare affected mappings. Resolve duplicate columns and explicit sums; shared metadata columns may be reused, quantity occurrences may not.
4. Resolve permanent driver identity, the selected date authority and true driver/day duplicates. The interface can introduce identity first, but duplicate detection requires date context.
5. Prepare categorical inclusion/exclusion and row aggregation where needed. New category values need review. Normalize one resulting quantity per Driver/day/pay item through canonical payroll rules.
6. Present organized dates/explanations, missing-coverage warnings, current/incoming values and unresolved identity/conflict/exception decisions.
7. In Slice B, resolve those decisions, confirm the impact, revalidate and apply through canonical payroll authority.
8. Complete the receipt/history and activate replacement saved relationships only with successful completion. Failed/unfinished work never displaces working rules.

Absent known columns retain their mappings. Changed external names beyond accepted normalization, internal display-name edits and relevant dependency changes require affected remapping only. The latest successfully completed relationship replaces the old relationship for that target; no source quantities are added to a contribution ledger.

If a Sum requires a missing column or blank input, produce no result for that row and warn; preserve existing payroll. Do not turn missing into zero. Eligible row aggregation must not turn an incomplete target group into a partial replacement. Identical completed data is a no-op even after a later manual edit; explain the prior completion/later edit without restoring old values. Actual changed data remains subject to ordinary conflict review.

Persist setup independently of original files. Recoverable corrections carry compatible draft mappings/filters and useful error state forward. Unsafe files are still rejected atomically, and finalization retention still applies.

## 5. Complete Slice A: twelve sequential stages

The stage identifiers **A01-A12** belong to this revised delivery plan. They do not rename, erase or mark the historical A1/A2 work complete under new requirements. Existing assets may satisfy portions of a stage only after focused verification against this contract. Complete and review each stage before its successor.

| Stage | Owned delivery | Dependencies | Completion gate |
|---|---|---|---|
| A01 | Central product/resource policy and authorized effective-policy metadata | Preserved baseline | Validated configuration, one source for defaults, backend/UI contract and changed-value checks |
| A02 | Canonical Company/Branch capacity governance and retirement semantics | A01 | Concurrent/effective-dated capacity checks preserve history and frozen periods; no new ordinal cap |
| A03 | Forward schema/rule-version, lifecycle, ownership, retention and staleness foundation | A02 | SQL/Alembic parity, tenant/version guards, atomic rollback and finalization cleanup before new intake exposure |
| A04 | Strict per-file table inspection and bounded canonical extraction | A03 | Size boundaries, formula/cache/type fidelity, hidden/headerless notices, no presentation-driven extent |
| A05 | One-source batch/header compatibility and combined occurrence accounting | A04 | Implemented and focused-verified: header-only preflight, repeated daily structures, partial overlap, pairwise compatibility, duplicate multiplicity and incompatible/over-limit atomic rejection |
| A06 | Pending per-target mappings, Direct/Sum and duplicate-header outcomes | A05 | Implemented and focused-verified: no quantity reuse; missing-input warnings; unchanged active rules; compatible selective reuse/remapping |
| A07 | Permanent Driver identification and discrepancy discovery | A06 | Implemented and focused-verified: Company code authority, bounded current-name proposals/discrepancies, same-name distinct Drivers, cross-Company isolation, stale-context recheck and no master/payroll writes; forward lookup index 0097 |
| A08 | Chosen date authority, supported inference and explainable date review | A07 | Implemented and focused-verified: column/filename/sheet authority, deterministic written-month and period-year inference, wrong-year refusal, bounded coverage review, persisted target date profiles |
| A09 | Repeated-row aggregation and categorical filter preparation | A08 | Implemented and focused-verified: typed category inspection/paging, explicit choices, exact grouped quantities, incomplete-group withholding, contributor provenance and zero payroll/head writes |
| A10 | Accurate compact preview, semantic repeat recognition and bounded reads | A09 | Implemented and focused-verified: target-scoped full-input receipts, repeat no-op preserving later manual values, source validation retained, version/revision/date paging, and receipt retention after purge; no payroll or head writes |
| A11 | Integrated backend review, revised workload and deployment acceptance | A10 | Prepared repeat-after-manual-edit, partial-column and canonical-byte-boundary workloads; user measurements, correctness/retention/concurrency review and authoritative operational-budget decision remain pending |
| A12 | Connected source-setup and read-only preview UI | A11 | Authorized end-to-end flow, effective policy display, recovery and bounded review; no final apply controls |

Each gate requires fresh focused evidence. Authorization, isolation, version rejection and no-payroll-write behavior remain cross-cutting from A03 onward. A11 does not postpone safety checks until after unsafe APIs exist. Slice A may configure draft mappings, dates, sums and filters; it does not confirm canonical identity overrides, accept payroll conflicts/exceptions, apply quantities or promote current successful rules. Those actions belong to Slice B.

The current request/proxy deadline and per-process memory budget are not established. Until revised workloads are evaluated against an authoritative deployment budget, synchronous suitability and the A11/A12 handoff remain unaccepted. A justified durable-execution design is a separate decision; no queues or polling are added speculatively.

## 6. Slice B: user decisions and atomic application

After A12 is accepted, implement user Driver choices/confirmations, category/row exclusions where required, keep-existing/use-imported conflict decisions, normal-day-off exception decisions and held full-day-off Status handling. Preserve the existing full-day-off review behavior: list affected Drivers once, none selected by default; accepted work removes only its conflicting Statuses through canonical Status authority and preserves notes. Deliberately excluded data does not apply; unresolved choices block completion.

Bind every decision to the current reviewed facts. Under canonical currency/period/Import lock order, reauthorize and revalidate editability, eligibility, current quantities/Statuses and rule revisions. Stale facts refuse the commit without writes. Use the existing Payroll-owned set-based quantity writer and canonical Status/evidence paths; do not add an Import-owned payroll writer.

Apply only actual reviewed changes, retain unchanged provenance, and preserve omitted values. Persist receipt/change/evidence links and activate affected immutable saved-rule versions in the same transaction as successful completion. Failure rolls back both payroll and rule activation. Support idempotent completion and read-only history.

Detailed Status-file import remains a later separate contract; resolving held Status conflicts during ordinary work import does not implement it.

## 7. Requirement ownership and acceptance matrix

Stable R/S/F references remain usable; revised meanings replace the prior product assumptions, whose text remains in Git history.

| Requirement | Primary owner | Verification/consumer |
|---|---|---|
| S1 strict CSV/XLSX daily-source intake | A04-A05 | A12 connected intake |
| S2 dynamic period template | A04/A10 | Must obey the same effective product/resource policy |
| S3 first-row table and two date authorities | A04/A08 | A12 date explanation/review |
| S4 reusable per-target Direct/Sum and duplicate resolution | A06 | A09 transforms; Slice B activates rules |
| S5 Driver matching and discrepancies | A07 | A10 eligibility; Slice B explicit acceptance |
| S6 canonical type/unit/date/eligibility validation | A08-A10 | Slice B revalidates under locks |
| S7 confirmed aggregation and current-payroll conflicts | A09-A10 | Slice B resolves/apply choices |
| S8 actionable issues, omitted-data notices and coverage warnings | A04-A10 | A12 bounded display |
| S9 whole-import preview/impact counts | A10 | A12 display; Slice B final confirmation |
| S10 revision/semantic-context checks | A03/A10 | Slice B commit-time revalidation |
| S11 identical completed-data no-op preserving later edits | A10 | Slice B idempotent completion/history |
| S12 atomic canonical payroll application | Slice B | Financial, eligibility, rollback and evidence checks |
| S13 receipt/history and accepted rule versions | Slice B | Retention preserves durable metadata |
| S14 value provenance and explicit exclusions | A03/A09-A10 | Slice B applied-change links |
| F1-F2 neutral envelope and reusable source/target processing; staging separate | A03-A10 | No parallel matcher or payroll authority |
| F3-F6 provenance, durable mapping, before/after evidence and distinct byte/semantic identity | A03/A06/A10 | Slice B audit/receipt; finalization cleanup |
| F7 separate Status domain | All stages | Dedicated Status import deferred |
| Configurable allowances and current usable-concept slots | A01-A02 | Changed policy, concurrency and retirement checks |
| Pending setup retention; successful-only mapping replacement | A03/A06 | Slice B atomic activation |

The previous L1 deferral of automatic mapping reuse is superseded: compatible Branch reuse is required in this V1. Global source-system alias/catalog infrastructure and a company-wide Import hub remain deferred. Identity acceptance reuse must stay within explicit scoped rules; DWR-023 is not marked complete by this documentation.

## 8. Preservation, migration and release boundaries

Keep the neutral Import envelope, immutable source ownership, compact source/normalized records, Driver/day preview, set-based reads, revision/semantic identity separation, guarded publication, retention and canonical bulk writer. Adapt these assets; do not merge the preview branch or recreate per-quantity relational staging.

Completed-import recognition runs during preview from versioned private target receipts retained in `ReviewSummary`; bounded same-Company/Branch/period history lookup refuses oversized or unsupported declared evidence rather than truncating it. A target matches only when its complete interpreted input multiset, frozen meaning and accepted source-rule meaning match; unrelated presentation/layout details and current edited payroll values are not input identity. Preview reports a bounded target notice and marks matched incoming candidates `AlreadyImported`/`NoOp`, while retaining current values and provenance. Invalid new source identity/date/category/quantity evidence still blocks matching. The receipt is not trusted while its Import is editable, and this stage adds no completion/apply writer or active-head mutation. Date review is revision/plan-bound and fully paged. These behaviors are implemented and focused-verified in A10; Slice B still owns successful completion receipts, applied-change evidence and atomic payroll application.

The paginated-read supported-contract-version gap was implemented and focused-verified in A03: metadata, rows, issues, and source-record pages use the shared version boundary, with editable preview rule-context freshness separated from terminal historical reads.

Schema head `0097` is current. `0096` remains the forward A03 migration from preserved `0095`; `0097` adds only the ordered Company/Driver identity lookup index. Future changes move forward from the actual current head while preserving canonical Payroll facts/evidence and history. Never rewrite `0094`/`0095`/`0096`, reset/reseed as an implicit upgrade, or add speculative obsolete-contract adapters. Existing pre-production data mismatches must be reported and reconciled explicitly, not silently deleted.

Acceptance requires focused stage checks, integrated ordinary-work scenarios, direct-SQL/Alembic equivalence when schema changes, retention/staleness races, lawful revised benchmark shapes and independent review. Historical measurements in [design §11](FLUSSRA_C2_IMPORT_DESIGN.md#11-measured-backend-evidence-and-remaining-gate) remain evidence for the preserved baseline only. Do not claim the revised product is implemented or performance-approved from those results. A full-backend run at `6ca4691` completed with 3,368 passed in 577.80 seconds (artifact `.artifacts/validation/manual-full-20261028-132025`); it does not supply A11 workload measurements. Do not routinely run the full suite; a final full-suite confidence check after focused work is left for manual execution under AGENTS.md.
