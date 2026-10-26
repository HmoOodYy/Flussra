# FLUSSRA C2 IMPORT PLAN

**Status:** Agreed product direction (2026-10-24), with Slice A delivery and operational decisions finalized on 2026-10-25. A1 is merged through PR #74 at 337e3f7. The 2026-10-26 batch/layout/compact-preview refoundation contract below supersedes the original operating assumptions; backend verification and bulk-write feasibility precede A3. A3 and public Slice B decisions/apply have not started. The [implementation design](FLUSSRA_C2_IMPORT_DESIGN.md) specifies the sequential mission gates and must satisfy the C2 gate in `FLUSSRA_UNIFIED_REFOUNDATION_EXECUTION_PLAN.md` §10.<br>
**Scope:** what Flussra Import is, where it starts, the rules it never breaks, what the first version contains, and what follows.<br>
**Terms:** *Pay Items* are the company's PayDefinitions as shown in the UI. A period's *frozen Pay Items* are the definitions frozen into that period at creation. *Daily entries* are the period's Daily Grid source rows.

Point IDs (R1, S4, L2, …) are stable so individual points can be accepted, changed or dropped by reference.

---

## 1. Starting point

The first import is **payroll work data, inside one payroll period**. The user opens a period, chooses *Add payroll data*, brings in a file of daily work (driver, date, quantities per Pay Item), reviews it, confirms it, and it lands in that period's daily entries.

Why it comes first:

1. It is the work that repeats every period; People and Driver Rates are entered once at onboarding.
2. It creates nothing. It only fills a period that already exists and is ready.
3. Its target already exists: the period has frozen its drivers, dates and Pay Items.
4. It forces the hard parts of the shared engine to be built and proven first: staging, conflicts, atomic commit, commit-time revalidation, history and provenance. People and Rate imports then reuse them.
5. For the pilot, People and Driver Rates can be entered by hand.

The idea the whole feature rests on:

- Import lives inside a payroll period, not on a separate Import page.
- The period defines what a file may contain: its drivers, its dates, its frozen Pay Items and their input rules.
- The file brings facts; Flussra determines pay.
- Nothing changes in payroll until the user confirms, and then everything goes in or nothing does.
- Every imported value remembers its file, sheet, row and column.

## 2. Rules (R)

| ID | Rule |
|---|---|
| R1 | The file is flexible, the target is strict. Users bring their own Excel; Flussra adapts to it. |
| R2 | No silent changes. Nothing is overwritten without being shown and decided. |
| R3 | PayrollWork/File values are traceable to their import, file, sheet, row and column while the period can change; after finalization, to the durable import and payroll audit record (D7). Other sources/targets define their own provenance and retention contracts. |
| R4 | For PayrollWork, the period is the target schema: not current settings or a fixed template. Importing into an older period uses that period's frozen Pay Items. Other Import kinds need not have a PayrollPeriod. |
| R5 | PayrollWork import never calculates pay. It supplies driver, date, Pay Item and quantity; the calculation engine does the rest. |
| R6 | PayrollWork v1 never creates Driver, Employee, PayDefinition or other master data. Future People Import may create/update only the entities permitted by the locked Workforce contract, through canonical Workforce authority. No import silently creates Access/User accounts or a parallel source of truth. |
| R7 | Pay Items are never imported. They are configured in Flussra only. A bulk Pay Item tool, if ever needed, is a separate configuration feature and not part of Import. |
| R8 | A Status in a file (for example PTO) belongs to the Status domain and never becomes a Pay Item. |
| R9 | Everything before confirmation is staging, not payroll truth. |
| R10 | All or nothing. There is no accidental partial import; the user may deliberately exclude rows. |
| R11 | PayrollWork Driver identification is separate from eligibility for the period/date. Both are checked by that target contract. Generic source records have no mandatory Driver match. |
| R12 | Missing coverage is not a value. Only mapped definitions enter a source's supplied set. Within that set blank means no incoming quantity and zero is explicit zero. A later partial Import never clears absent definitions. |
| R13 | Suggestions may assist mapping, but nothing decides payroll automatically. Anything ambiguous needs a person. |

## 3. Version 1 — included (S)

| ID | Included |
|---|---|
| S1 | One batch can contain multiple .xlsx/.csv files; one XLSX can contribute multiple selected worksheet source units, read by reusable application boundaries. |
| S2 | Two ways in: upload your own file, or download this period's Flussra template. |
| S3 | Multiple source-unit selection, visible header interpretation, and accepted WorkDate from a column, filename, sheet name or explicit fixed unit/Import context. Inference is a suggestion until accepted. |
| S4 | Deterministic reviewable source-layout grouping; compatible units share one accepted mapping, heterogeneous groups use different mappings. Targets and grid columns derive dynamically from frozen period definitions. |
| S5 | Driver matching: stable driver identifier first; an exact name with a single candidate is a suggestion the user confirms; ambiguous names block until the user chooses. |
| S6 | Validation: value type and unit per Pay Item, date inside the period, driver eligible for that period and date. A normal day off is still a valid date. |
| S7 | Collisions: same value is unchanged; an empty cell is new; a different value is a conflict resolved as *keep existing* or *use imported*, with *apply to all similar*; multiple source values for the same canonical target within one Import are a blocking duplicate across all files/units/layouts; no implicit aggregation. |
| S8 | Precise, human error messages naming the row, field, value and reason. |
| S9 | Impact preview (new, changed, unchanged, skipped, conflicts resolved, errors) and a confirm button that states the number of changes. |
| S10 | Commit-time revalidation: the period is still editable and nothing reviewed has changed, otherwise the preview must be refreshed. |
| S11 | Re-uploading an already completed import changes nothing and says when and by whom it was done. |
| S12 | Atomic commit into the period's daily entries, marked as imported. |
| S13 | An import receipt and the period's import history: file, user, time, counts and the mapping used. |
| S14 | Value-level provenance from each imported value to its batch, file, row and column. |

These are the complete C2 v1 capabilities, not permission to deliver them all in Slice A. A1–A3 deliver upload/interpretation/mapping and a read-only payroll-impact preview. Slice B delivers user decisions and confirmations, conflict/exception/exclusion actions, applying changes, receipt and history (§10). Slice A can display an unresolved proposal or conflict but cannot decide it.

### Batch processing and capacity

One period may be completed incrementally through many partial Imports. One Import
is a user-submitted batch, not one file and not the whole payroll period. Multiple
layout groups may supply different definition subsets in the same batch.

Operational capacities are configurable and apply to the complete batch. They
are not schema/business maxima. The [design §8](FLUSSRA_C2_IMPORT_DESIGN.md#8-configurable-resource-policy)
records initial operating defaults and enforcement: per-file and aggregate
bytes/decompression, files/units/layouts, selected rows/rectangles, mapped
candidates, cell text, multipart overhead and admission. At least 31 daily files
or worksheets must be supported; 31 is a minimum verification case, not a maximum.
The old 10-sheet product maximum is retired. A retained million-cell rectangle
is parser/security policy, not a million relational payroll-cell target.

150 Drivers/Branch, 20 active definitions and a 31-day month are the design/performance
center (93,000 potential facts). A rare approximately 10,000-row/200,000-candidate
case is also measured. These figures belong only in verification fixtures, never
schema constraints, DTO fields, production mapping rules or business limits.
Identity/reference/ignored columns are distinct from Pay Definition count.

Processing is synchronous-first, with parsing, normalization, comparison and
publication separated from HTTP routes. Same-request bounded thread offload is
permitted. Do not add queues/jobs/workers/polling speculatively. Compare the
corrected architecture to A1/A2 evidence and look for an authoritative interactive
request/deployment budget. No such budget may be invented. If suitability cannot
be established without that authority, state it; if durable work is justified,
stop before A3 and return the smallest required worker design as the next task.

Preview is a bounded Driver + WorkDate grid with dynamic supplied-definition
fields, whole-import summaries, paginated issues and exact source drilldown.
It is not a human row-per-fact list. Omitted definitions are absent; mapped blanks
and explicit zero remain distinguishable. Source units and accepted interpretation/
mapping membership remain visible. Formulas use cached values only, with missing
required caches reported as errors.

## 4. Version 1 — excluded (O)

| ID | Excluded | Reason |
|---|---|---|
| O1 | People import, Driver Rate import, driver creation | Separate imports that follow (N2, N3) |
| O2 | Pay Item creation, Bonus, Minimum/Maximum, final pay | R5, R6, R7 |
| O3 | Status import | Designed for (F7); decided after real pilot files |
| O4 | Imports spanning several periods | One import belongs to one period (§6) |
| O5 | Direct integrations, file formats beyond S1 | After customer evidence |
| O6 | Undo | The data it needs is stored from the start (F5) |
| O7 | AI mapping | Deterministic mapping comes first |
| O8 | A company-wide Import Hub | Import history lives inside each period (§6) |

## 5. Version 1 simplifications (L)

Each is a smaller first step toward a capability the target design keeps. None blocks the later step, because the data it needs is stored from the start.

| ID | Target capability | Version 1 | Later |
|---|---|---|---|
| L1 | Saved Source Profiles that map recurring files automatically | Each import stores its mapping; it is not reused automatically | Reuse the last mapping, then full profiles with source-format change detection |
| L2 | Smart column suggestions | Exact header matching plus manual mapping | Known aliases and suggestions |
| L3 | Saved external driver aliases (source system + external key + Driver; DWR-023) | A2 resolves stable identifiers and records per-import matches/proposals; Slice B owns user choices/confirmations for unmatched cases | Remember resolutions as aliases across imports; not A1–A3 scope |
| L4 | Correcting values inside Flussra | Resolve conflicts, choose drivers and exclude rows in Flussra; wrong values are fixed in the file and re-uploaded | Inline cell editing in the review table |
| L5 | Template recognition and drift detection | Exact template headers map automatically; a hidden period stamp is deferred | Hidden period stamp and reporting which fields changed since the template was generated |

## 6. Placement

- One Import belongs to one period/Branch; one period may receive many partial Imports. Each batch may contain many files/source units/layout groups and any supplied subset of frozen definitions. Rows outside the period are flagged, never routed to another period.
- Import history is shown inside the period. A company-wide Import Hub follows only when there is something to manage across periods (saved profiles, aliases, People and Rate imports), and then as *Data Imports / Import History*.
- Onboarding is led by the Setup Center (Set up business → Set up payroll model → Run payroll). Import appears inside the step that needs it rather than as a destination of its own.

## 7. Version 1 flow (C)

```
C1  Open the period → Add payroll data
C2  Upload source-file batch   or   download a dynamic period template
C3  Select source units; accept headers and WorkDate providers
C4  Review layout membership; accept each layout's identity/date/quantity mapping
C5  Match drivers: identifier → confirmed name → user choice
C6  Validate: type, unit, date, eligibility, cross-file/unit/layout duplicate target facts
C7  Review: valid, warnings, errors, decisions (conflicts, full-day-off Statuses, day-off work); decide or exclude
C8  Impact preview → Import N changes
C9  Revalidate at commit → commit everything at once
C10 Receipt → links to the Daily Grid and the updated payroll preview
```

Payroll is unchanged until C9.

Persisted import lifecycle: `NeedsMapping → NeedsReview → Completed`, with `Cancelled` for an uncompleted import. A failed synchronous request rolls back rather than persisting a processing job or `Failed` state. Readiness is computed from validation and outstanding decisions, not a background status. A1–A3 never mark an import `Completed`.

Slice A covers C1–C4 and the discovery/validation/display portions of C5–C8. Driver choice/confirmation in C5, all decision/exclusion actions in C7, final confirmation in C8, and C9–C10 belong to Slice B. Mapping a column, selecting interpretation or cancelling the whole staged import does not decide or apply a payroll change.

## 8. Foundations required from the start (F)

| ID | Requirement | What it keeps open |
|---|---|---|
| F1 | One shared engine; each import type supplies its own contract (Period now, then People and Rates) | People and Rate imports without a second system |
| F2 | Staging separate from payroll | Future integrations enter the same staging engine as files |
| F3 | Source/target-specific provenance: File coordinates and PayrollWork value/evidence links until payroll finalization, then durable import/audit metadata (D7) | Other source/target kinds supply their own provenance and retention trigger |
| F4 | File/PayrollWork imports permanently store accepted interpretation (sheet/header/date order and CSV encoding/delimiter), source-layout mappings including Driver-identifier column roles and accepted date providers, and mapping/content identity in the owning specialized contracts. Per-identifier Driver-match rows remain detailed staging under D7 | S13 reports the mapping used; L1 can reuse persisted mappings without Driver/File fields in the generic core. Saved Driver aliases remain the separate later L3 capability |
| F5 | Before and after values stored for every change | A later safe Undo that never reverts over newer manual edits |
| F6 | File byte fingerprint on the File source; versioned semantic content/plan fingerprints on the owning target | S11 duplicate protection and L5 drift detection, separate from creation idempotency and resource revision |
| F7 | Status data routed to the Status domain | Status import without touching Pay Items |

## 9. What follows (N)

| ID | Next | Why in this order |
|---|---|---|
| N1 | L1–L5 upgrades | Faster recurring imports on the same engine |
| N2 | People import in the Setup Center | Onboarding at scale. A spreadsheet never grants Flussra access; imported people start as *Not invited*. Worker type is not a permission. Flussra's own identity stays separate from the company's employee code. |
| N3 | Driver Rate import, with a template generated from the company's Pay Items | Requires drivers and Pay Items to exist |
| N4 | Status import | After real pilot files |
| N5 | Undo, direct integrations, AI suggestions | When the pilot shows the need |

## 10. Sequencing

1. Daily Grid UX, enough that a payroll user reads it at a glance. Import is verified in the Daily Grid, and the import review table shares its layout (drivers down, days and Pay Items across).
2. Import version 1 (§3–§8), delivered through the gates below.
3. The broader Flussra visual redesign, after the pilot shows what matters.

A1's merged security foundation is retained. Before A3, the compact multi-file/layout backend, read-only preview and canonical bulk-write feasibility must be implemented and reviewed under the [refoundation design](FLUSSRA_C2_IMPORT_DESIGN.md). Unmerged A2 is evidence only and is not merged wholesale. A3 remains the connected UI; Slice B remains the downstream user-decision/apply workflow.

| Mission | Owned delivery | Completion gate |
|---|---|---|
| **A1 — Safe upload and staging** | `0094`; safe synchronous CSV/XLSX ingress; actual-content validation and all hard limits; staging/inspection; sheet/header selection and interpretation metadata; access controls; required finalization cleanup; bounded benchmark | Files can be safely uploaded, inspected, interpreted and staged without changing payroll or canonical payroll facts; cleanup exists before upload exposure and measured evidence supports synchronous bounds |
| **Backend refoundation / accurate preview** | Multi-file/unit/layout ownership; partial supplied coverage; context WorkDates; compact staging; permanent-Driver-first resolution; semantic identities; combined dynamic grid; atomic refresh/retention; bounded reads; configurable aggregate admission; internal canonical bulk-write feasibility | Correctness/security/schema checks and measured representative/design-center/rare heterogeneous workloads; synchronous suitability stated against real budget authority; bulk canonical writes preserve constraints/audit/evidence; no public apply |
| **A3 — Connected user flow** | Upload → Mapping → Preview UI; selected sheet/header/date interpretation; useful errors; bounded pagination/filtering; supported request recovery/retry; end-to-end preview workflow | An authorized user can reach a reliable preview from file selection without deciding or applying any payroll changes |
| **Slice B — Decisions and commit** | User choices/confirmations; conflict/exception/exclusion actions; final impact confirmation; atomic apply/revalidation; provenance, SourceType, receipt and history | The existing complete v1 decision/commit contract works after A1, A2 and A3 have each passed review |

A1 owns the existing D7 finalization cleanup before any upload is exposed as an available product workflow. Detailed data from never-completed or cancelled imports must be purged too. A2/A3 reuse and verify that lifecycle; Slice B adds completed-import provenance verification without moving apply into Slice A. Retained metadata must not fabricate completion or applied changes.

[DWR-023](FLUSSRA_DEFERRED_WORK_REGISTER_EN.md#dwr-023--external-driver-identifiers-and-aliases-are-deferred-to-c2-import) is a design input for A2's company-scoped stable-identifier → permanent Driver → effective Branch/date assignment resolution. Name-only cases remain proposals until Slice B confirmation. The retired dormant import tables are not the new matching authority. L3 still defers saved source-system aliases across imports; this mission split neither creates a generic alias framework nor marks that deferred implementation complete.

## 11. Decisions (D)

D1–D7 were decided on 2026-10-24. D8–D20 record the settled delivery, operational and foundation decisions of 2026-10-25.

| ID | Decision |
|---|---|
| D1 | There is no automatic default for a conflict: the user decides each one, and may apply a decision to all similar conflicts. |
| D2 | An imported value never replaces an existing value silently, whatever put that value there (a manual entry or an earlier import): the user chooses whether to overwrite it. |
| D3 | An **error** is incoming data whose destination — driver, target, date, pay item or any other required destination — Flussra cannot determine with confidence. Errors block the import. A **warning** is data Flussra understands and can place, but that looks unusual. Where understood data cannot be accepted without a choice, Review asks for it; the user either decides or deliberately excludes the affected data and continues with the rest, and an unresolved choice blocks completion. |
| D4 | Normalize source rows to Driver + WorkDate and supplied definitions. WorkDate need not be a row column: accepted file/sheet/fixed context is supported. Many daily units and heterogeneous definition subsets combine in one Import. Other wide day-column reader modes remain later extensions. |
| D5 | Imported work on a day whose Status is a full day off is resolved inside Review, never by sending the user out of Import. Review lists the affected drivers once each, nothing selected by default, with select all. Selecting a driver removes the conflicting full-day-off Status where imported work exists and accepts that work; otherwise that work is not accepted and is deliberately excluded. |
| D6 | Imported work on a normal day off asks whether to treat it as an exception for this period. Yes accepts it and Review notes that day-off exceptions were accepted; No excludes it and the rest continues; unanswered blocks completion. |
| D7 | The original file and individual source-row/cell provenance serve Import and Review until the period is finalized; they are not permanent payroll data. After finalization Flussra keeps accepted payroll data and durable import/audit metadata, including accepted File interpretation and target column mapping/identity for S13/L1/F4. File bytes, inspection samples, row/cell/Driver-match detail and transient preview/current-plan state are purged. Selected sheet/header interpretation is durable metadata, distinct from per-data-row coordinates. |
| D8 | Merged A1 security foundation -> compact batch/layout backend and accurate preview plus bulk-write feasibility/review -> A3 UI/review -> public Slice B decisions/apply. No downstream UI investment bypasses backend correctness/workload gates. |
| D9 | Aggregate configurable file/unit/layout/row/candidate resource policy protects the entire Import, retaining A1 content/ZIP/XML/formula/text protections. Counts are operational, not schema/business caps. Verify at least 31 daily units; retire the 10-sheet product cap and million relational-cell acceptance workload. |
| D10 | Synchronous-first reusable processing, outside HTTP-domain logic. Benchmark the compact architecture and canonical bulk writes before A3. Use authoritative request/deployment budgets when available; state their absence instead of inventing a threshold. Unsuitable evidence blocks acceptance; a durable-worker design is a separate justified next task, not speculative infrastructure. |
| D11 | Preview is bounded: whole-import summaries, paginated rows/issues and error/warning/conflict filters. A2 records and enforces numeric page/response bounds based on existing conventions and measurement. |
| D12 | Show selected sheet, selected/detected header and relevant date interpretation before results are relied upon. Preserve identifier text where practical; use cached formula values without evaluation/recalculation, and report missing required caches as validation errors. |
| D13 | Slice A may resolve exact stable identifiers automatically and discover/display unresolved matches/conflicts, but makes no user decisions, confirmations, user-directed resolutions or payroll changes. Those actions remain in Slice B. A1 owns D7 cleanup before upload exposure, including never-completed imports, without importing Slice B apply behavior. |
| D14 | The shared Import envelope owns neutral identity/scope/kinds/lifecycle/revision/idempotency/version metadata. File sources own file/interpretation/provenance; PayrollWork targets own period/mapping/Driver matching/plan state. Source records/issues are neutral, and multiple identifier associations per PayrollWork source record preserve disagreement detection. No universal matcher or ETL platform is built. |
| D15 | Import reads return revision. Interpretation/cancel and later mapping/refresh/decision/commit mutations require expected revision, update atomically and reject stale callers. Revision and PlanFingerprint protect different kinds of staleness. |
| D16 | Upload creation requires a client Idempotency-Key. Same Company key and identical versioned creation identity returns the existing Import; changed identity conflicts; a new key deliberately creates another Import. File SHA is not the key and this is distinct from S11. |
| D17 | Persist envelope, source and target contract versions, plus the PayrollWork canonicalization version before A2 hashes exist. Unsupported staged versions fail closed and require reset/re-upload, without compatibility adapters. |
| D18 | Bound actual route-local bytes for the complete ordered multipart file batch, each file and aggregate overhead. Use short authorization/persistence transactions with no DB transaction or period lock during receive/hash/parse. Reauthorize and revision-check under period-before-import locks before accepted persistence. |
| D19 | Import owns retention invariants; Payroll finalization invokes the PayrollWork cleanup hook in its existing transaction. Failure rolls back the complete finalization. Other targets choose their own retention trigger; payroll finalization is not generic Import lifecycle authority. |
| D20 | XLSX safety also includes generous, measured and boundary-tested internal package/member and XML nesting ceilings, separate from the existing product limits and actual decompressed-byte cap. |

Additional settled refoundation decisions (2026-10-26):

| ID | Decision |
|---|---|
| D21 | File is not Import. Multiple files/units/layouts normalize into one scoped batch and combined preview. |
| D22 | Unmapped definitions are outside supplied coverage; blank/zero apply only within that coverage. No later partial Import touches absent definitions. |
| D23 | Compatible units share reviewable accepted mappings; heterogeneous layouts map independently. Filename/sheet date suggestions require explicit acceptance. |
| D24 | Compact source/normalized records replace relational per-quantity preview rows. Human preview is Driver + WorkDate with dynamic definition fields and provenance drilldown. |
| D25 | Preserve Company permanent identity before date eligibility and versioned business-semantic fingerprints independent of surrogate IDs/audit timestamps. |
| D26 | Prove an internal Payroll-owned set-based ordinary quantity boundary, retaining canonical validation, locks, Status conflicts, source semantics, audit/evidence and atomicity. No public Import apply or decisions in this work. |

## 12. Existing import tables

Historical migration 0094 retired the dormant predecessor imports/integrations and established A1. Main remains the baseline authority. Corrected migration 0095 cuts disposable A1 staging over to the compact batch/unit/layout model without modifying canonical Payroll facts. Unmerged A2's unrelated 0095 is not reused. See [design §4](FLUSSRA_C2_IMPORT_DESIGN.md#4-compact-schema-and-cutover).
