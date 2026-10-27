# Flussra C2 Import Design — Strict Reusable Ordinary Work

**Authority:** Implements the [canonical product contract](FLUSSRA_C2_IMPORT_CONTRACT.md) and [complete delivery plan](FLUSSRA_C2_IMPORT_PLAN.md), revised 2026-10-27. A01 configurable policy, A02 canonical capacity governance, A03 schema, rule lifecycle, version boundaries, retention and staleness foundation, and A04 strict per-file canonical extraction are implemented and focused-verified; A05-A12 remain planned. Slice A has twelve sequential stages A01-A12; Slice B owns final decisions and application.

**Preserved baseline:** `codex/c2-import-foundation`, implementation `9e03a66`, documentation through `f15bf96`; merged historical A1 at `337e3f7`, schema head `0095`. Preserve these commits and canonical facts/evidence. Divergent preview `6498453` is reference only. The existing compact backend and Payroll-owned bulk boundary are useful implementation assets, not evidence that the revised source/rule/UI contract is already built. Historical workload evidence remains in §11.

## 1. Domain boundaries

One Import is one submitted source setup for one Company/Branch/editable payroll period. It can contain several compatible daily files and supply only some frozen inputs. The period is the target schema. Normalize to one DriverAssignment + WorkDate + PayrollPeriodDefinition quantity; the current payroll calculation engine supplies money, including existing ordinal behavior. No new ordinal topology or tier-count ceiling belongs to this C2 delivery.

The neutral Import envelope owns scope/lifecycle/revision/idempotency/correlation. File-source code owns bounded extraction and physical provenance. PayrollWork owns input rules, identity/date eligibility, transformations, supplied coverage and payroll comparison. Canonical Payroll owns quantity/Status mutations and evidence. No source-system business catalog, universal matcher, additive contribution ledger or separate payroll writer is introduced.

Status-file import is dedicated, text-only and separately designed later. An ordinary-work categorical filter does not import Flussra Status. Existing held-Status/day-off conflicts still require canonical review. People/Rate/Access imports, master creation, spreadsheet recalculation, inline source-value editing and speculative job infrastructure remain outside this delivery.

## 2. Sources and canonical extraction

### 2.1 Strict shape and provenance

Accept CSV/XLSX with one sheet/file, one table/sheet, first-row headers and no merged cells in the imported table. Reject unsupported workbook/report shape without truncation. Keep immutable file ordinal/name/type/size/byte digest and physical sheet/row/column coordinates; repeated filenames remain distinguishable by their ordinal. Do not count styled empty cells as table data. Hidden populated columns are inspected, counted and identified. Headerless data is ignored with the product notice, excluded from headed-table allowance, and still bounded by inspection safeguards.

Initial compatibility uses normalized headers only: trim surrounding whitespace and compare case-insensitively; retain original labels and occurrence positions for explanation. No payroll-value sampling decides source similarity. Corresponding named occurrences in compatible daily files share combined capacity; duplicate labels within one file remain separate occurrences. The combined union includes each distinct label's necessary occurrence slots, not the sum across files. Apply the configured combined-column allowance and require explicit resolution when duplicate count/arrangement changes. Reject completely disjoint structures and unsupported mixed setups; do not infer their business relationship.

Source-layout metadata may remain an internal structure/provenance tool. It does not authorize an interface for multiple independent source families, arbitrary regrouping or multiple selected sheets in one setup.

Discover first-row headers and check shape/column capacity as early as bounded inspection allows, before materializing complete row-value payloads. Once batch headers are available, reject incompatible or over-capacity combined structures before payroll interpretation; do not scan values to decide header similarity.

### 2.2 Canonical payload and size

Apply the upload-byte allowance before bounded archive/XML/CSV processing. Strict intake reads row 1 as the only header row and permits exactly one workbook sheet per file, including hidden and chart sheets in that count. Explicit multiple Excel tables, declared table headers below row 1, and merged cells intersecting the headed table are refused. CSV and undeclared worksheet input use the row-1-headed grid; no blank-row or value-similarity heuristic guesses business tables.

Extract headed cells into a versioned deterministic UTF-8 JSON representation: fixed serialization/key ordering, ordered header occurrences, original labels, physical row/column locators and typed values. Preserve exact numeric lexemes, formula text, cache presence/result and semantic date/time/duration types; do not first round through binary floating-point. Record genuine blanks distinctly from unavailable formula results. Hidden headed columns remain present with their physical coordinates and disclosure markers. Headerless data is omitted from the canonical payload and disclosed after inspection, while its actual values remain subject to physical row/column/cell/text/decompression safeguards. Presentation-only fonts/colors/borders/styles/validation metadata do not extend the data rectangle or enter canonical bytes.

For CSV and XLSX, preserve blank physical row coordinates between headed data rows. Canonical rows extend only through the final physical row with a value or formula in a headed column; later rows containing only headerless data are excluded from canonical bytes and the admitted `imported_row_count`. Physical inspection `row_count`, `column_count`, and `cell_count` continue to enforce resource safeguards over ignored data. This rule records a headed grid and does not infer multiple business tables. Include all headed columns before mapping, even those later set to Ignore. Measure actual uncompressed canonical UTF-8 bytes against the configured per-file allowance. Never use a regenerated workbook's compressed size, DB TOAST size or mapped-column-only size for admission. Canonical format versions are explicit protocol contracts, not tunable numeric limits.

Canonical format 1 has top-level fields `format_version`, `file_type`, `unit_name`, `unit_index`, `column_count`, `header`, and `rows`. Each header occurrence and cell retains its physical column, typed value and original label where applicable; optional formula metadata holds formula text/attributes and cache presence, type, raw value and usability. Styled Excel date/time cells retain the exact serial lexeme and epoch beside an ISO view. That view truncates sub-microsecond fractions so a value just before midnight cannot round into a different date. Duration cells keep their exact Excel serial with `encoding: \"excel_serial\"`; this preserves source meaning without adding a payroll duration parser. A cached empty string (`t=\"str\"`) is a usable blank, while an empty numeric cache placeholder is unavailable. These cache distinctions follow [SpreadsheetML cell value semantics](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.cellvalue).

Keep original bytes immutable under the existing retention policy. Store admitted canonical data/size/digest/version under File ownership so mapping/refresh can use typed extraction without repeatedly opening the original workbook. Canonical detail follows original-file finalization purge. Preserve original workbook-derived type meaning before discarding style metadata.

CSV has literal text values; formula-looking text is not executed. XLSX formulas use saved cached results only. A needed formula without a cache is an error; a real cached blank remains distinguishable. Unsupported domain quantity formats remain invalid rather than receiving a new Import-specific parser.

### 2.3 Safety and correction

Retain actual-content checks; bounded multipart headers/body; ZIP paths, encryption, member count, CRC and actual inflated-byte checks; DTD/entity/XML-depth protection; macro rejection; exact coordinates and bounded text. Do not trust claimed MIME, Content-Length or worksheet dimensions as sole authority. Parsing/extraction runs off-thread under bounded admission without an open payroll transaction/period lock. Reauthorize and recheck period editability when staging is persisted.

Reject unsafe/over-limit uploads atomically. For recoverable validation failures retain pending setup and useful diagnostics. A corrected upload uses a new immutable manifest/creation identity and carries only compatible pending setup forward; it does not overwrite the old file or reuse an idempotency key for changed bytes. Re-upload/correction must not force unaffected relationships to be configured again.

## 3. Accepted meaning and transformations

### 3.1 Target relationships and lifecycle

Maintain immutable versioned Branch-specific relationships for individual PayDefinitions, with a current-successful head per target. A rule references the external column occurrence(s), Direct/Sum transform, identity/date dependencies, optional repeated-row/filter handling and relevant confirmed target/source labels. Bind it to the receiving period's frozen definition when used; do not persist one old period-definition ID as universal mapping authority.

Pending rules are Import-owned proposals. They never replace current successful heads before Slice B successfully completes. A target already reserved by an existing rule requires explicit replacement; activating a new relationship supersedes the old one without additive accounting. Other targets retain their heads. Original-file purge must not remove current rules or immutable accepted rule metadata needed by history/reuse.

Absent known columns preserve relationships and produce missing coverage as appropriate. Changed external labels beyond allowed normalization, changed internal display names and relevant dependencies require affected remapping. Unique named columns may be located by their normalized labels; duplicate occurrences require their accepted count/arrangement signature. Never silently align changed duplicates by ordinal alone.

### 3.2 Direct, Sum and duplicate headers

Direct maps one quantity occurrence to one target. Sum maps explicit eligible numeric occurrences to a result, then validates that result against the target. Shared Driver/Date/Filter fields may support several mappings; a quantity occurrence is consumed once. Duplicate identity/date columns are not Sum candidates and need explicit chosen/ignored outcomes. Every duplicate occurrence must have an outcome before continuation.

Parse numeric operands exactly and retain invalid-value diagnostics. Do not impose the abandoned binary-prefix restrictions. Apply canonical target validity to the resulting quantity: finite precision, nonnegative value and whole-number ordinal quantity. Import never evaluates pay rates or money.

A missing Sum column or blank operand yields no result for that source row and a warning, not zero or a partial sum. Invalid nonblank operands remain errors. Explicit zeros are valid supplied operands. If an included repeated-row group contains an incomplete Sum result for a target, withhold that Driver/day/target result and warn; publishing only the other rows would silently treat unknown input as zero. Other fully understood targets can still be supplied under the reviewed batch contract.

### 3.3 Driver identity and date authority

Resolve permanent Company Driver/Employee stable keys set-wise, separately from Branch/date eligibility. Code/name agreement is ordinary; known code with name variation requires explicit acceptance. Distinct codes do not merge because names match. Conflicting known identities block; an unknown code cannot be repaired automatically by a name. Exact unique name-only matches remain proposals. Preserve text/leading-zero cautions and Company isolation; do not create Drivers or invent aliases. Remember only explicitly confirmed scoped setup, not inferred identity overrides.

Offer two user-facing date authorities: file/sheet name or a selected Date Column. Persist which name provider is chosen if filename and sheet name differ; competing date-like row values never override it. In column mode one file may supply one or several work dates. Manual date resolution is allowed for genuinely unrecognized/ambiguous input, not as a silent rewrite of explicit wrong-year data.

Retain current unambiguous numeric-date handling; add supported written-month extraction and confirmed scoped patterns. Clean supported date-bearing text such as `O.c_t (Samsara) 16`, infer an omitted year only from an unambiguous period fit, and preserve the interpretation reason. Explicit wrong year/out-of-period dates are errors. Compare recognized sequences for coverage; never link an unrecognized file to the last missing day. No numeric confidence or unrestricted-language model is required. Duplicate detection runs only after date context exists, regardless of screen order.

### 3.4 Repeated rows and categories

Before applying repeated facts, require a confirmed rule: sum by resolved Driver Code within day, or include selected categorical values then sum within that Driver/day. Inspect the selected column, expose distinct scalar values and occurrence counts, and require explicit inclusion/exclusion. Confirm categorical suitability from the actual repeated-value structure; do not classify HOS semantics or invent a fixed distinct-value threshold. Page large category lists under effective response/resource bounds.

Previously confirmed excluded values ignore the whole applicable source row. New categorical values, including an unrecognized blank/type variant, require explicit review rather than implicit exclusion. Preserve category values for review; external-header case normalization does not authorize silently merging category values. Keep traceability of included/excluded rows and selected columns.

Reduce complete eligible contributions to one Driver/day/pay-item quantity. Validate the final quantity through canonical payroll domain rules and apply the existing ordinal calculation once downstream. Missing/invalid identities or dates cannot become an aggregation bucket. No automatic sum is selected merely because repeated rows exist.

## 4. Compact schema and cutover

`0094` is historical merged foundation; `0095` is the preserved compact batch schema. `0096` is the current A03 head, forward from `0095`; direct-SQL and Alembic catalog comparison reports no application-schema differences. Future changes must be forward migrations from the actual current head. Do not rewrite earlier migrations, run implicit reset/reseed, copy preview's divergent migration, or delete canonical Payroll/audit/evidence/history as reconciliation. Detect unsupported staged versions and report a scoped reset/re-upload requirement; do not invent compatibility adapters or pretend old metadata already satisfies new rules.

Retain Imports, FileSources/source units, compact neutral/source records, PayrollWork records/matches and Driver/day preview groups. Extend File ownership with canonical payload/version/byte identity. Add durable Company/Branch/target-owned immutable rule versions and current-head references, pending per-Import rule references/revisions, sparse review decisions and durable applied-change/evidence links. No fixed columns per pay item, per-value staging table or source-system catalog is needed.

Composite ownership must cover Company/Branch/Import/period/definition and every rule/head relationship. JSON payload shape and frozen definition membership remain validated; moving configuration into JSON does not waive ownership checks. Record explicit source/target/canonicalization/rule versions. The version compatibility guard applies to metadata, every preview/issues/source read, mutation and final commit.

Each durable applied-change link references the exact `(ImportID, PayDefinitionID, RuleVersionID)` pending-rule version through a validated composite foreign key. This prevents later pending-rule edits from changing which immutable rule version is associated with an already-written payroll change.

Do not revive the retired dormant matcher/integration schemas. A02 capacity enforcement requires no schema change. Keep schema, route and seed authority checks synchronized with the forward migrations owned by A03 and later stages.

## 5. Reusable processing and atomic preview

1. Authorize scope/non-driver actor in a short transaction; release it before receive/extraction/materialization.
2. Admit the complete manifest against effective per-file and aggregate safeguards; stage immutable original/canonical File data atomically.
3. Load pending or applicable successful rule versions and canonical typed payloads without holding mutation locks.
4. Resolve identities/dates and source configuration, transform complete quantities, load canonical context set-wise and compute compact normalized records/issues/hashes off-thread.
5. Prepare bounded transfer streams outside the write transaction. Avoid N+1 payroll queries, Day Grid screen/calculation reads and a second full candidate matrix.
6. Reauthorize, lock the period before Import, recheck revision/editability and complete relational context, then atomically replace derived preview data/counts/fingerprints.
7. Close invocation-owned buffers/temp resources on success, rejection, disconnect and rollback. Readers observe complete old or complete new preview state.

Source/rule changes advance revision and invalidate dependent preview/decisions. Preserve compatible pending setup on failure. A stale preparation cannot overwrite newer interpretation, rules, payroll context or finalization. Capacity rejection is actionable/retryable, never an unbounded queue. Processing remains synchronous-first; do not add durable execution or polling without a justified separate design.

## 6. Semantic identities, repeats and preservation review

Keep byte/creation identity, canonical-file identity, mapped content identity, reviewed plan identity and relational publication staleness distinct. Use versioned deterministic serialization, exact decimals, blank/zero/cache distinction and duplicate-preserving ordering. Generated IDs/audit times do not become public semantic meaning; relational IDs remain ownership references.

Mapping identity covers target frozen meaning, relevant confirmed labels/occurrences, transform/filter/date/identity dependencies and rule version semantics. Unrelated absent columns do not invalidate a target. Content identity covers the complete interpreted mapped input, including repeated-row multiplicity and meaning; ignored/presentation data does not become a new payroll fact. Do not identify repeats solely by equal final totals, one page, filenames or file SHA.

Before offering changes, recognize a completed equivalent business import within the same Company/Branch/period/target and accepted-rule meaning. Identical completed data is a no-op even after a manual payroll edit: preserve that edit/provenance, explain the completed import and create no fake source mutation/evidence. Changed actual mapped input uses normal comparison/review. Creation retry with the same client key and exact ordered manifest returns existing metadata; a changed manifest conflicts. A new key alone does not make old business data payable again.

**Completed-import notice gap: pending implementation/preservation review, A10.** Preserve the reference test intent for byte/semantic recognition and completed-by/when explanation, adapting it to revised batch/rule semantics and the manual-edit no-op. Do not blindly restore the old single-file notice API.

The A03 shared supported-contract boundary now covers metadata, rows, issues, source-record pages and mutations. Editable preview pages also validate their stored pending-rule context against current relevant heads; Completed and Cancelled imports retain historical reads after later head changes while their own versions and revision/fingerprint remain checked. A10 still owns future completed-import recognition and repeat/no-op behavior described above; the paginated-read version gap is implemented and focused-verified.

## 7. Human preview, APIs and UI boundaries

Retain a bounded Driver/day grid with dynamic supplied frozen inputs, exact current/incoming values and complete counts. Missing definitions are absent; mapped blanks, explicit zero, invalid and excluded data remain distinguishable. Provenance and issue/source drilldown must expose every retained candidate through bounded pages, not only summary samples. Do not return the whole workbook.

Retain creation/detail/preview/rows/issues/source-record/template/cancel service boundaries. Revise their DTOs for one source setup, strict headers, per-target versioned Direct/Sum rules, duplicate occurrence outcomes, category configuration and the selected date authority. Remove multi-sheet/arbitrary-header/regroup choices from the revised supported product interface. Internal source-layout storage is not public permission for the old workflow.

Add authorized read-only effective product/resource policy metadata, consumed by UI messages, upload hints and mapping limits. Bounds are server-enforced and versioned; no independent frontend copies. Extend revisioned draft source-configuration interfaces for the new rules. Slice B alone adds explicit canonical Driver decisions, conflict/exception/exclusion actions, final confirmation/commit and receipt/history reads. DTO validation must obtain adjustable capacities from policy rather than fixed list sizes.

Every mutation requires expected revision, editable scope and relevant rule versions. Every page binds to one supported contract/revision/whole-preview fingerprint and consistent read snapshot. Paginate summaries/targets/headers/categories/issues/source data under effective count/byte limits, reporting exact totals and next offsets. New policy values cannot enable an unbounded response.

Register every eventual route in authorization inventory. Mutations require payroll.entry and Company/Branch/non-driver boundaries; reads use existing payroll.view/payroll.entry rules. The A12 interface supports setup and read-only preview; it shows unresolved business decisions but cannot pretend a preview is applied or render nonfunctional apply controls.

## 8. Configurable resource policy

| Product setting | Initial default | Owner |
|---|---:|---|
| Uploaded bytes/file | 10,000,000 | File intake |
| Canonical headed-table bytes/file | 1,000,000 | File extraction |
| Combined source-column occurrences | 40 | Source setup |
| Currently usable Company pay items | 50 | Canonical definition governance |
| Branch input concepts including Status | 15 | Effective Branch applicability |

Centralize defaults once; existing Settings loads deployment overrides and supplies validated immutable product/resource policy objects. Product growth changes configured allowances, not database cardinality CHECKs, fixed pay-item fields or duplicated route/UI constants. Expose effective values and stable reasons to authorized clients through `GET /payroll/periods/{period_id}/imports/policy`. The projection reports configured allowances. A02 enforces canonical Company/Branch capacities; source-shape limits remain assigned to A04/A05. No policy editing UI or company entitlement system is required.

Keep separate operational policy for actual batch files/upload bytes/decompressed bytes, physical columns/rows/cells/candidates, text/XML/package work, preview page/response bytes and concurrent heavy requests. Preserve applicable existing defaults until revised realistic benchmarks justify final aggregate values; do not claim all maximum combinations fit. Avoid unrelated numerical cleanup outside these boundaries.

Capture effective admission-policy identity for diagnostics/preview validation. A policy value increase alone does not change a source's business meaning or invalidate compatible saved mappings. New operations obey current safety/capacity checks; existing completed history and frozen payroll snapshots are never rewritten because a limit changes. If a lowered allowance makes a pending operation unacceptable, return the actual current limit and retain recoverable setup rather than truncating.

Count currently usable definitions in canonical direct-create/approval/activation/retirement writers, serialized with compatible scope locks and existing global lock order. Branch checks use effective-dated applicability and include Status once, not once per selectable Status value. Enforce proposed future activation states so deferred changes do not create a later over-capacity branch. Retirement releases usable slots without deleting structure/rates/provenance or changing frozen periods. Preserve existing data and report capacity mismatches; do not silently retire items or reset data to meet a new default.

Storage precision, protocol versions, supported formats, one-sheet/table structure and mathematical validity are explicit contracts, not deployment knobs. Retain exact canonical payroll normalization and existing ordinal topology; no configurable six-tier ceiling is introduced in C2.

### 8.1 Effective policy API and configuration

`GET /payroll/periods/{period_id}/imports/policy` uses the ordinary Company/Branch period-read authority, denies Driver subjects, and requires `payroll.view` or `payroll.entry`. It opens a read-only transaction and returns typed `policy_version`, `policy_fingerprint`, `company_id`, `branch_id`, `period_id`, `product_policy` and `import_resource_policy`. It returns no application settings, connection strings or secrets. The deterministic fingerprint binds versioned effective policy values, not actor/period identities, and is separate from payroll mapping/content fingerprints.

`ProductPolicy` centrally owns the Company/Branch and future canonical-file/combined-column allowances. `ImportResourcePolicy` centrally owns operational defaults, including existing upload admission, page default/maximum sizes, source-detail/header samples and response bytes. `Settings` references those defaults and accepts deployment overrides. Product keys are `PRODUCT_MAX_COMPANY_PAY_ITEMS`, `PRODUCT_MAX_BRANCH_INPUT_CONCEPTS`, `IMPORT_MAX_COMBINED_COLUMNS` and `IMPORT_MAX_CANONICAL_FILE_BYTES`; existing upload/resource keys retain their `IMPORT_` names. Paging settings use `IMPORT_<ROW|ISSUE|SOURCE|SOURCE_CELL|TARGET|HEADER>_PAGE_DEFAULT` and `_PAGE_MAX`; `IMPORT_MAX_RESPONSE_BYTES` controls serialized Import response bounds.

Configuration requires positive integer capacities and page defaults no larger than their maxima; invalid configuration fails before the application serves requests and must not print secret settings in validation errors. Omitted page limits resolve from effective policy, explicit limits are checked against its maximum, and response-byte enforcement includes final metadata/continuation fields. Changing environment configuration requires application re-creation/restart; there is no runtime policy-edit endpoint. Policy objects are immutable, and the UI contract reads effective values instead of copying literals.

This API reports configured product allowances; canonical Company/Branch count enforcement is implemented in A02, and per-file canonical byte/shape admission is implemented in A04. Cross-file combined-column semantics remain assigned to A05 and are not implied by policy metadata. A03's shared supported-contract boundary now covers metadata, rows, issues, source pages and mutations. Editable preview reads also validate the current pending-rule context; terminal history remains readable after later head changes. Existing operational admission/response bounds consume the effective resource policy now; historical workload evidence in §11 does not validate arbitrary increased settings.

## 9. Retention and Slice B canonical application

Import owns detail cleanup, invoked inside canonical finalization after the period lock. Lock Imports in deterministic order and clear `FileCanonicalData.Payload` before purging original `FileSources.FileContent`; keep canonical format version, byte count, digest and purge timestamp, plus the original file manifest/digests. Purge inspection samples, source/normalized records, transient match/issue/group rows, current plan detail, `PayrollWorkReviewDecisions` and `PayrollWorkPreviewRuleContexts`, including for cancelled/never-completed imports. Preserve `PayrollWorkRuleVersions`, `PayrollWorkPendingRules`, `PayrollWorkRuleHeads`, `PayrollWorkAppliedChanges` and their linked Payroll evidence. Aggregate `ReviewSummary` counts and completion/audit metadata survive. Failure rolls back complete finalization; finalized periods cannot recreate detail. Current Branch setup must survive file purge.

Slice B binds explicit decisions to reviewed incoming/current values, Status/day state, identities and affected rule-head revisions. Revalidate under canonical currency -> period -> Import lock order. Any rule-head locks used for final activation follow that order and a deterministic target order; no inverse path or late Branch-configuration lock may be introduced. Staleness refuses everything atomically.

Use canonical Status authority to remove only explicitly accepted conflicting full-day-off Statuses, preserving notes, then the existing Payroll-owned set-based quantity writer for reviewed adds/changes/explicit zeros. Keep source validation, date eligibility/rescue, Status conflicts, actual-change-only audit/SOURCE evidence, shared correlation and unchanged provenance. Do not call interactive per-cell writes thousands of times or disable guards.

Actual imported changes use `SourceType=Import`; unchanged facts retain their current provenance. Manual/public quantity writes must use `Manual`, including manual edits of imported values, rather than trusting a client-supplied Import label. Apply these source semantics through canonical writers and retain their regression coverage in Slice B.

In one transaction, persist actual-change/evidence links, receipt, successful completion and current-head activation for the affected pending rule versions. No-op quantities create no fake evidence; completed-data repeats preserve later manual edits. Failed/stale/uncompleted imports do not promote mappings. Missing rows/values and incomplete sums never clear existing payroll. Receipt/history and permitted purge must remain truthful about what actually completed.

## 10. Twelve-stage Slice A and complete acceptance

A01-A12 are the revised sequential delivery identifiers. Historical A1/A2/A3 describe the earlier three-mission baseline only. Each stage depends on the preceding accepted/reviewed stage; baseline code can be reused only with fresh evidence for its revised responsibility.

| Stage | Owned delivery | Required focused verification |
|---|---|---|
| A01 | Central product/resource policy and authorized effective-policy metadata | Startup validation; changed defaults/overrides; backend/UI metadata consistency |
| A02 | Canonical Company/Branch capacity governance and retirement semantics | Concurrent creation/activation; effective-date boundaries; retirement frees slots; frozen history unchanged |
| A03 | Forward schema/rule-version, lifecycle, ownership, retention and staleness foundation | SQL/Alembic parity; scope/version guards; purge/rollback/finalization races |
| A04 | Strict per-file table inspection and bounded canonical extraction | Exact upload/canonical byte boundaries; styles/hidden/headerless data; formulas/cache/precision/types; unsafe packages |
| A05 | One-source batch/header compatibility and combined occurrence accounting | Repeated daily files; partial overlap; duplicate count; disjoint/over-limit atomic rejection |
| A06 | Pending per-target mappings, Direct/Sum and duplicate-header outcomes | Missing-input skip/warn; invalid operands; no quantity reuse; unaffected/current rules preserved |
| A07 | Permanent Driver identification and discrepancy discovery | Known code/name mismatch; unknown code; same-name different codes; isolation and no master writes |
| A08 | Chosen date authority, supported inference and explainable date review | Written months/patterns/year inference; wrong-year rejection; chosen-provider authority; no missing-day guesses |
| A09 | Repeated-row aggregation and categorical filter preparation | Correct date context; explicit rules; new values; exclusions; incomplete-group withholding; final ordinal quantity |
| A10 | Accurate compact preview, semantic repeat recognition and bounded reads | Whole-content hashes; current facts/eligibility; repeat after manual edit; version/revision/page/provenance completeness |
| A11 | Integrated backend review, revised workload and deployment acceptance | Real guarded workloads, locks/memory/ordinary-read responsiveness; independent review and actual budget decision |
| A12 | Connected source-setup and read-only preview UI | Authorized setup/preview/recovery; effective limits; bounded pages; zero payroll writes and no apply controls |

A03 establishes retention and scope/version protection before revised intake exposure. Every stage keeps isolation and zero payroll-source writes; A11 is not permission to postpone those checks. Draft transform/category/date configuration is preparation; canonical identity/conflict/exception acceptance, current-rule promotion, final application and receipt completion remain Slice B after A12 acceptance.

Benchmark realistic compatible daily CSV/single-sheet XLSX, partial-column variants, lawful input counts, at/beyond canonical-byte boundaries, issue-heavy and repeated-row/category cases. Record total/phase time, locked publication/refresh/purge time, memory/temp behavior and authenticated read responsiveness with all guards/audit/evidence active where applicable. Compare like inputs; do not relabel the historical multi-sheet/20-definition scenarios as revised acceptance.

Synchronous HTTP suitability remains unestablished without an authoritative request/proxy deadline and per-process memory/concurrency budget. A11 cannot pass the deployment gate, and A12 cannot bypass it, on an invented threshold. If measured evidence justifies durable execution, return a separately approved minimal design; do not implement jobs/polling speculatively.

Slice B acceptance additionally proves user decisions, existing day-off/held-Status behavior, stale refusal/no writes, exact reviewed changes/evidence/receipt, active-rule atomicity, idempotent completion/manual-edit preservation, rollback and completed-import retention. Dedicated Status import and a new ordinal tier cap remain separately deferred. Follow AGENTS.md focused verification; a final full-suite confidence run is manual, not an automatic implementation runner.

## 11. Measured backend evidence and remaining gate

> Historical baseline evidence from implementation 9e03a66. Every result, acceptance statement, capacity, internal section reference and A1/A2/A3 label in this section refers to that earlier design. It does not certify the revised contract or mark A01-A12 complete. The historical A3 UI gate corresponds to the revised A11/A12 acceptance boundary. Measurements and reproduction details are retained verbatim below.

Measured 2026-10-26 on Windows 11 x64, 12 logical processors, Python 3.12.10 and PostgreSQL 18.4. The scripts use a disposable production-schema PostgreSQL cluster, real application services through HTTPX ASGITransport, ordinary authenticated period GET probes, and production constraints/guards/audit/evidence. Each Import workload has a fresh process, so working-set peaks include interpreter/fixture overhead but do not carry forward another scenario's peak. These are development-host observations, not network/proxy measurements or statistical production SLOs; repeated runs varied with host load/cache state.

Final isolated-workload pass (seconds; peak working set in decimal MB):

| Workload | Source rows / quantity candidates | Preview request | Refresh request | Publication transaction* | Refresh transaction* | Purge component | Peak MB |
|---|---:|---:|---:|---:|---:|---:|---:|
| Representative CSV: 20 Drivers, 7 days, 4 definitions | 140 / 560 | 0.28 | 0.26 | 0.13 | 0.12 | 0.07 | 127.7 |
| Monthly CSV: 62 daily files, 2 heterogeneous layout groups, 150 Drivers, 31 days, 20 definitions | 9,300 / 93,000 | 14.64 | 16.66 | 6.34 | 8.37 | 2.21 | 355.3 |
| Monthly XLSX: 31 daily worksheets, same design-center facts | 4,650 / 93,000 | 16.58 | 18.19 | 3.68 | 5.34 | 1.33 | 318.4 |
| Rare CSV: 1,000 Drivers, 10 days, 20 definitions | 10,000 / 200,000 | 22.38 | 27.03 | 7.86 | 15.04 | 3.47 | 552.1 |
| Issue-heavy CSV: same rare shape, every quantity invalid | 10,000 / 200,000 | 35.07 | 27.24 | 12.74 | 8.65 | 2.12 | 755.8 |

*Transaction timing is connection begin through COMMIT dispatch, including final context recheck/publication; it excludes the COMMIT round trip. The raw evidence separately records lock-to-COMMIT-dispatch and lock-to-response intervals. Purge measures the Import-owned cleanup component under a finalized-period lock, not financial snapshot/ledger finalization; real finalization and rollback are separately covered by focused integration tests.

First-preview phase attribution (seconds; context is both initial read and final recheck and therefore overlaps the publication transaction):

| Workload | Upload request | File materialization | Both context reads | Normalize/compare/hash | Serialize/prepare outside write transaction | Guarded publication |
|---|---:|---:|---:|---:|---:|---:|
| Representative CSV | 0.22 | <0.01 | 0.04 | 0.04 | 0.01 | 0.09 |
| Monthly CSV | 0.49 | 0.45 | 0.29 | 5.67 | 1.74 | 6.02 |
| Monthly XLSX | 3.92 | 6.90 | 0.33 | 4.18 | 1.48 | 3.40 |
| Rare CSV | 0.35 | 0.74 | 4.06 | 8.83 | 3.14 | 5.09 |
| Issue-heavy CSV | 0.46 | 0.94 | 5.53 | 13.02 | 5.48 | 9.57 |

The monthly CSV combines both layout families into 4,650 Driver/day grid rows. The rare/issue-heavy scenarios publish 10,000 normalized records and 10,000 grid groups, not 200,000 relational cell/issue rows. Per-value issues stay in record JSON; generic ImportIssues has zero rows in these scenarios. Guarded COPY uses 1,000-row chunks (46, 26 and 51 calls for monthly CSV, monthly XLSX and rare workloads respectively). All preview/refresh cases leave canonical DraftLines and SOURCE evidence unchanged. Responses stay within 2,000,000 bytes; invocation temp snapshots and database temp statistics recorded no newly retained files/spill bytes.

Ordinary authenticated GET median latency during first preview was approximately 6–8 ms. Observed maxima were 0.06 s representative, 0.61 s monthly CSV, 0.52 s monthly XLSX, 0.56 s rare and 1.77 s issue-heavy; refresh maxima were 0.02/0.56/0.57/0.54/0.92 s respectively. Thread offload therefore preserves concurrency but does not eliminate CPU/GIL/GC latency spikes. Full high-workload concurrency and production memory capacity are not certified by these single-workload measurements.

### 11.1 Diagnosed costs and remediation

- Source parsing uses one openpyxl workbook per selected physical XLSX and row-bounded XML metadata, including exact numeric/cached-value lexemes; typed date/time/duration handling remains distinct.
- Complete normalization/comparison and canonical hashing now dominate application CPU and peak memory. Per-row payload serialization runs off-thread before the mutation transaction, then discards duplicate raw/semantic/value Python structures. No second full serialized candidate matrix is held during publication.
- Binary COPY streams guarded relational source/record/group/match rows in bounded chunks. Group issue counts are computed before publication and inserted directly. The model scales by source records and meaningful ownership identities rather than guarded quantity cells.
- EXPLAIN ANALYZE identified a cold-statistics FK planner trap during refresh/purge: a broad Import-scoped btree lookup repeatedly visited uncommitted deleted child rows instead of constraining the group key. In the 10,000-group profile, the ownership FK consumed about 10.43 s and the Import writable guard about 0.28 s. The corrected `0095` uses a BRIN Import-scope lookup for appended records while retaining selective group/source btrees; the same FK checks fell to about 0.16 s with every constraint/guard active. Scope-leading covering variants were measured and rejected because they moved or retained the point-lookup cost. BRIN locality and index tuning should be revisited if future staging churn changes the physical workload.
- Refresh still atomically retires/replaces all derived detail; finalization still owns one-way purge. No partial preview, disabled guard, deferred constraint or lowered product capability is used to improve timing.

The historical 939,953-cell workload and its 100.75 s publication/approximately 25 s purge are retired product-acceptance shapes. The new measured scenarios intentionally contain fewer facts and a different persistence grain; the numbers are not a same-input speedup claim.

### 11.2 Canonical bulk feasibility

The internal Payroll-owned authority was measured with 93,000 reviewed targets, real source constraints/Status guards, ordinary audit and immutable SOURCE evidence enabled:

| Operation | Total seconds | Locked seconds | SQL calls + COPY batches | Actual changes |
|---|---:|---:|---:|---:|
| Create | 15.97 | 15.95 | 303 + 93 | 93,000 |
| Update | 30.26 | 30.18 | 303 + 93 | 93,000 |
| Unchanged | 2.04 | 1.96 | 20 + 93 | 0 |
| Void | 19.45 | 19.38 | 298 + 93 | 93,000 |

Peak process working set was approximately 387.3 MB. The three mutating operations produced 279,000 ordinary audit rows and 279,000 immutable SOURCE events; unchanged values created neither. A pathological outer current-state join was replaced with an inner set-wise read, and measured empty/no-op DML batches were removed: create/update/unchanged/void fell from 28.94/46.98/32.90/29.09 s to the table above. The corrected current-state reads took roughly 0.35–0.44 s after source existed. The boundary is feasible without the interactive per-cell loop, but its atomic write duration is a real deployment-budget consideration. It is not a public Import apply workflow.

### 11.3 Acceptance state and reproduction

**Synchronous HTTP suitability is not established. A3 remains gated.** No authoritative request/proxy deadline or per-process memory budget exists in the checked repository configuration (`backend/app/config.py`, `backend/app/main.py`, `scripts/dev_backend.ps1`). The measured rare/issue-heavy latency, publication lock duration, CPU spikes and memory/admission envelope must be assessed against that deployment authority before product exposure. No latency threshold, durable-worker requirement or lower product limit is invented here. If the deployment budget later rules out these bounded synchronous workloads, the next task must explicitly design durable execution/claims/retries/revision-safe publication before A3.

The operating defaults in §8 include the verified 62-file/31-sheet and 200,000-candidate shapes with headroom; they remain protective configurable ceilings, not certification of every possible maximum combination or business limits. Saved external aliases remain DWR-023 NOT YET. A3 UI and public Slice B decisions/confirmation/apply remain unimplemented.

Private publication-context invalidation is deliberately conservative across the selected-date canonical read: an unrelated period edit can require a retry even when the public supplied-fact plan would be unchanged. Public comparisons and semantic fingerprints remain limited to mapped/supplied facts. Narrower invalidation can be assessed with real concurrent-edit workload evidence; it is not a reason to weaken stale-publication checks.

Final focused verification passed 211 tests in 74.74 s across the Import modules, canonical quantity batch, isolation enforcement and route authorization inventory. The final source-projection/current-format and semantic-fixture changes additionally passed all 34 affected checks. Fresh direct-SQL/Alembic comparison at the single `0095` head reported zero unexplained catalog differences; populated canonical payroll/evidence preservation, canonical reset/seed, configured CI test Ruff, changed backend Ruff, compileall, diff and local documentation links were checked. The full backend suite was not run; this does not claim independent-review/full-CI acceptance.

From `backend`, using Python 3.12 and the repository's canonical temporary-directory configuration, reproduce with `python -m scripts.benchmark_import_files --scenario all` and `python -m scripts.benchmark_quantity_batch --drivers 150 --definitions 20 --days 31`. Use `--scenario rare_csv --profile-cleanup` for the real FK/guard EXPLAIN profile (instrumentation adds overhead). Raw JSON/logs default to ignored `.artifacts/`; the tables above retain the engineering evidence in the authoritative design.
