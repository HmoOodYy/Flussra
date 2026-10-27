# Flussra C2 Import Product Contract

**Status:** Agreed product decisions, 2026-10-27. This is the canonical contract for the revised Import behavior, reconciled with the complete twelve-stage Slice A and Slice B delivery documents. A01-A05 have been implemented and focused-verified on the foundation continuation; A05 adds cross-file header compatibility and combined-column accounting, not mapping semantics or the complete revised import experience/payroll application.

**Continuation baseline:** `codex/c2-import-foundation`, implementation at `9e03a660b2fa06c4e2aa498715e4cacee2773f6e` and documentation through `f15bf96`. Preserve its implementation and history. The divergent `codex/c2-import-preview` at `6498453` is reference evidence only; it is not to be merged or cherry-picked as part of recording this contract.

**Authority:** This document records product decisions; the companion [C2 product plan](FLUSSRA_C2_IMPORT_PLAN.md) owns delivery and the [technical design](FLUSSRA_C2_IMPORT_DESIGN.md) owns engineering boundaries and historical evidence. They now describe one revised ordinary-work contract. Superseded assumptions remain in Git history, not as competing current instructions. A rule recorded here is not by itself a claim that it is implemented; implementation status is stated separately above and in the plan.

## 1. Purpose and boundaries

Import supplies work facts to an existing editable payroll period. The period's frozen definitions, driver/date eligibility and existing payroll domain remain authoritative. Import prepares quantities; the payroll engine calculates money. Import does not create master data, invent a new ordinal model or create a parallel payroll writer.

One period can receive multiple partial imports. One successful import does not have to provide every payroll input or complete every day. Imported facts remain subject to existing authorization, Company/Branch isolation, eligibility, conflict review, atomic application, commit-time revalidation and audit/evidence requirements.

Status belongs to its own domain. Ordinary work import and Status import are separate contracts and separate files (§10).

## 2. One source setup and supported file shape

### 2.1 Meaning of a source setup

- Understand and finish **one source per setup**. A different source gets a separate setup afterward. A setup may include multiple files representing that source's days.
- A source is the user-confirmed input structure and its mapping rules. This contract does not require a new business catalog of named source systems or automatic discovery of relationships between different files.
- Source columns represent payroll inputs and identity/date/filter/reference information. A header such as `Monday`, `January` or `2026-01-05` may be an ordinary quantity name; header text never supplies a date or implies a day/month axis. Mapping source columns to days is unsupported. Date authority is chosen separately: file/sheet name or a Date Column (§7).
- Reject explicit requests to map or interpret source columns as days, including when such a request is mixed with otherwise supported files. Header names alone never establish that interpretation. Explain the accepted shape, that other payroll inputs can be added through a later setup, and that confirmed compatible setup can be reused.

### 2.2 Tabular restrictions

V1 accepts normal, organized `.xlsx` or `.csv` tables:

1. One sheet per file; no multiple-sheet workbook input.
2. One table per sheet.
3. The first row is the header.
4. No merged cells inside the imported table.
5. No arbitrary report layouts or multiple tables.

Presentation-only cells do not extend the import table. Formatting, styles, fonts, colors and data validations in empty rows/columns do not make them data.

Data beneath an absent header is ignored automatically, without requiring a decision for each such column. After inspection, show a clear notice, for example: **Some data was ignored because no column header was provided. Please review your file.** This is disclosed omission, not hidden data loss. Ignored headerless columns do not consume the 40-column allowance; inspecting them still falls under separate physical-input/resource safeguards.

Populated hidden columns participate in inspection and mapping, count toward the column allowance, and must be clearly identified as hidden. Visibility does not hide actual data from validation. A possible future color/style-based Import type requires separate study and is outside this contract.

### 2.3 Header compatibility and the 40-column limit

Initial structure comparison examines **column names/headers**, not column contents. Do not inspect sample payroll values to decide initial header similarity.

External header matching ignores capitalization and surrounding whitespace: `Hours`, `hours` and ` Hours ` are equivalent labels. Preserve enough original label/position information to explain mappings and distinguish duplicate occurrences. Other equivalence transformations are not authorized by this rule. Internal display-name changes still require affected remapping (§4.3). Header-only preflight checks every pair of files: each pair must share a normalized label, so a third file cannot bridge two disjoint structures.

- Repeated daily structures count once: `Driver | Code | A | B` in six files is four columns in the combined structure, not twenty-four.
- Partially overlapping structures may participate in the same source setup. `Driver | Code | A | B` and `Driver | Code | A | C` have a combined count of five occurrences.
- Files with completely different column sets are rejected from the same setup.
- The combined structure must contain at most **40 columns**, including identity, name, code, date, filter and other included columns, not merely pay-item quantities. Corresponding columns in repeated compatible files share a slot in that structure; separate physical columns within one file never collapse merely because their normalized labels match.
- For each normalized label, count the greatest occurrence multiplicity in any one file; the combined count is the sum of those per-label maxima. Thus `Driver | Code | Hours | Hours | Hours` consumes **five** columns, and another daily file with the same structure does not double the count to ten. Adding a `Bonus` occurrence adds one. Physical columns within a file retain distinct occurrence identifiers/signatures; the three Hours occurrences still require explicit resolution in §5.
- Reject an incompatible or over-limit setup as a whole; do not silently truncate or partially accept it.
- Count the imported header structure during bounded header-only preflight, before canonical row extraction or payroll interpretation. Format-only empty columns do not count.

Separate physical-column, row/cell and batch ceilings must account for inspection work, including ignored data. These resource safeguards are distinct from deduplicating corresponding columns across compatible daily files. A changed duplicate count or arrangement still requires renewed resolution; counting does not make an incompatible duplicate rule reusable.

## 3. Upload, canonical data and formulas

### 3.1 Two per-file limits

| Boundary | Initial configurable maximum |
|---|---:|
| Uploaded file | 10 MB per file |
| Extracted canonical import data | 1 MB per file |

The repository's existing decimal byte convention applies: 1 MB = 1,000,000 bytes. The upload ceiling is enforced before extraction. Extract a canonical import representation and measure it before the actual mapping/payroll interpretation proceeds. Do not generate a cleaned Excel workbook merely to measure its compressed size.

Canonical import data includes all headed table data before mapping: headers, cell values, formulas, cached formula results and types needed for interpretation. Include headed columns the user may later choose to Ignore; that mapping choice does not reduce the admission measurement. Headerless data excluded from the import table is excluded from this representation, while its inspection remains resource-bounded. Exclude presentation-only colors, fonts, borders, styles, formatting, data validations and other unnecessary presentation metadata. Preserve interpretation meaning derived from the original workbook, including date/time/duration types; discarding display styles must not discard semantic type information.

Canonical data at or below the effective canonical-file allowance (initially 1 MB) is eligible for further processing; above it is rejected. Extraction is itself bounded parsing work, not a cost-free operation. Retain applicable file-content, archive/decompression, XML, numeric-precision and resource protections while extracting. This product revision does not authorize removing security checks or making any batch unbounded.

Use the technical design's versioned deterministic UTF-8 JSON representation, retaining ordered occurrences, physical provenance, exact typed values and formula/cache distinctions. Measure its actual uncompressed bytes, not workbook/DB compression or a mapped subset. Keep canonical format versions explicit and distinct from configurable capacities. No new final batch maximum is selected here.

### 3.2 Formula results

- Preserve formula information and its final saved/cached calculated result in canonical import data where applicable.
- Import uses the saved/cached result. Flussra does not evaluate or recalculate spreadsheet formulas.
- A formula needed for an imported value without a saved/cached result is a validation error; that value cannot be accepted as blank or zero.
- Validate the cached value through the same target-domain rules as an ordinary value.

### 3.3 Batch resource policy

Final ceilings for total files, rows, cells and canonical/processed batch bytes remain **open until realistic workloads are benchmarked**. The 1 MB per-file policy does not establish safe aggregate cost. Existing configured safety ceilings remain implementation safeguards, not newly approved final product limits. No performance guarantee or asynchronous execution requirement is inferred from the new per-file limit.

### 3.4 Adjustable limits and policy authority

The stated upload/canonical-byte, combined-column, Company pay-item and Branch input-concept values are initial product defaults, not scattered constants or permanent schema maxima. Centralize defaults once, load deployment settings into validated immutable policy objects, and enforce them on the backend. Authorized policy metadata supplies UI values, hints and rejection messages; no independent frontend hard limits or customer policy editor is required.

Keep product allowances separate from operational file/physical-column/row/cell/archive/XML/response/concurrency safeguards. Raising an allowance must not require new fixed input fields, schema reconstruction or rewriting compatible mappings. Lowering an allowance does not rewrite completed imports or frozen payroll history; current operations must meet current safeguards and report recoverable capacity failures without truncation. Mathematical validity, supported formats, one-sheet/table structure, storage precision and protocol versions remain explicit contracts rather than arbitrary growth knobs.

## 4. Mapping and saved relationships

### 4.1 Target-oriented mapping

Show the payroll period's existing inputs and let the user map the inputs supplied by this source. A source may supply A and B while C, D and E arrive in later imports.

Each payroll target is linked to its external column or selected quantity transformation and relevant interpretation rules. A relationship is not owned by the whole physical file as an indivisible mapping. A later source exposing `2b | 3b | 4b` does not automatically invalidate unrelated relationships previously understood from `1a | 2a | 3a`.

Direct mapping supplies one input quantity. Explicit Sum can combine eligible numeric source columns into one result, which then maps to one target. Do not treat multiple raw columns as independent direct assignments to the same target or introduce general-purpose spreadsheet arithmetic.

### 4.2 Exclusive current relationship

- If a target is already linked, show that it is reserved and make moving its relationship explicit.
- The newest successfully confirmed mapping for that target becomes current. It replaces the previous relationship; there is **no additive multi-source contribution model in V1**.
- An old superseded structure requires mapping again before reuse. Merely uploading an old file does not restore its previous relationship.
- A replacement becomes current only after the import successfully completes with all required validation and decisions satisfied. Failed or unfinished setup remains pending and does not displace the working relationship.
- Replacing a mapping does not itself authorize overwriting payroll facts. Actual incoming changes still require the appropriate review/confirmation against current payroll.

### 4.3 Reuse, absence and change

Preserve mapping, sum/filter rules and confirmed setup independently of original uploaded files. Explain that keeping the confirmed internal and external names/structure avoids repeating setup.

Saved setup is **Branch-specific in V1**, within its Company. A confirmation in one Branch does not automatically authorize reuse in another Branch. This scope also governs the reusable source interpretation associated with that setup; it does not create a Company-wide source catalog. Company-wide sharing would require a separately explicit capability. Per-period application still uses the receiving period's frozen domain authority.

- An absent known column means this upload did not supply that input. Its saved relationship remains valid; missing coverage may warn.
- A changed external name beyond the accepted case/surrounding-whitespace normalization, such as `Miles` becoming `Total Distance`, must not be assumed equivalent. Remap only the affected relationship.
- A changed internal display name also requires affected remapping, even if the underlying internal identifier is stable.
- Changed relevant structure or interpretation dependencies require renewed resolution for the affected mapping, rather than a whole-file reset of unrelated mappings.
- Header stability is a reuse condition, not proof that an external system's units or business meaning have not changed. Import must not invent an interpretation of an unconfirmed semantic change.

## 5. Duplicate headers and explicit sums

Duplicate header labels are supported only through explicit resolution. `Hours | Hours | Hours` must be distinguishable as physical occurrences, for example **Hours #1**, **Hours #2**, **Hours #3**.

- Block continuation until every duplicated occurrence has a defined outcome.
- If duplicated columns contain numeric data and the chosen target supports that aggregation, the user may select which occurrences to sum and which to ignore.
- Driver Code, Date and other shared structural/identity columns are not numeric Sum candidates.
- This does not authorize silently selecting one of several identity/date columns. Duplicate structural occurrences require explicit resolution too.
- A previously accepted duplicate rule cannot be reused when the duplicate count or arrangement changes; ask the user to resolve it again.
- Ordinary Sum exposes a list of selected input blocks with an add-more control. It has no special binary 0/1 restriction.
- If a Sum requires an absent column or blank input, skip that row's target result with a warning and preserve existing payroll; do not sum only the available inputs or assume zero. Invalid nonblank operands remain errors and explicit zeros remain supplied values. An included repeated-row group with an incomplete target result must not publish a partial replacement for that Driver/day/item.
- A quantity input column occurrence already consumed by one quantity mapping cannot be reused elsewhere in that setup. Shared Driver, Date and Filter columns may be reused by multiple mappings.
- The final sum must obey the target's canonical quantity validation. An ordinal result must be a whole number greater than or equal to zero.

Identifying duplicate occurrences and their arrangement is part of saved rule identity. Unique named columns can be resolved by normalized label; duplicate count/arrangement must match the accepted signature or be resolved again. Do not silently make duplicate occurrences interchangeable.

## 6. Driver identity, repeated rows and filters

### 6.1 Identity authority

Driver Code is the higher authority; names can be similar or written differently. Match against existing canonical Flussra drivers before accepting payroll facts. Driver identity and Branch/date eligibility remain distinct checks.

| Incoming identity | Required treatment |
|---|---|
| Known code and matching name | Normal matching/eligibility validation |
| Known code and different name spelling/order | User acceptance is required; do not silently accept the discrepancy |
| Same name with different codes | Do not assume one person |
| Code and name point to different existing people | Block for explicit resolution; no silent acceptance |
| Unknown/nonexistent code with a matching name | Error/manual resolution; the name cannot automatically repair the code |

Preserve identifier text and existing Company isolation. No automatic driver creation or speculative code correction is authorized.

### 6.2 Repeated rows within date context

A driver code appearing on different dates is not a duplicate. Resolve date context internally before determining true repeated driver/day records; the user need not understand the detection mechanism.

At a real repeated-row case, block continuation until the user defines the handling:

1. Sum rows by Driver Code within the resolved day; or
2. Use Driver Code plus a selected categorical column to choose the rows eligible for summing within that day.

For the categorical option, inspect the chosen column's values, verify suitability for a categorical rule, show its distinct values, and let the user confirm included and excluded values. Previously confirmed excluded values cause the corresponding rows to be ignored. A newly encountered value must be shown for explicit resolution; never silently discard it.

Flussra does not need a built-in understanding of HOS statuses for this rule. An external categorical filter is not the separate Flussra Status import.

Aggregate eligible repeated rows into **one quantity per Driver / WorkDate / Pay Item**. Preserve source traceability and deliberate exclusions. Neither repeated rows nor duplicate headers authorize automatic summation before the rule is confirmed. Categorical suitability criteria and saved rule identity need detailed design; do not invent a domain classifier or a fixed distinct-value threshold.

## 7. Dates and repeat-import presentation

### 7.1 User-selected authority

Offer these two choices:

1. **Use file/sheet name as date.** That authority supplies the file's work date; date-like values inside its rows do not compete with it.
2. **Use Date Column**, selecting the column. It can hold the same date throughout or dates for consecutive days within one file; file/sheet-name dates do not override it.

Retain the single-sheet-per-file restriction in both modes. A manual resolution of a genuinely unclear date is allowed; this does not authorize retaining every older fixed-date-provider configuration as a new product option without reconciliation.

### 7.2 Assisted interpretation and hard boundaries

- Clean date-bearing text and extract supported date candidates, including written month names and previously confirmed source patterns. A label such as `O.c_t (Samsara) 16` is intended to be understood when the cleaned evidence is clear.
- Use the payroll period to infer an omitted year only when unambiguous.
- An explicitly written wrong year is rejected, never rewritten to fit the period.
- Compare recognized dates to the actual period-day authority and inspect the sequence for missing/duplicated coverage in the relevant source context.
- Never invent a missing day or suggest a date solely because it is the only missing period day. Even one unrecognized file and one missing day must be linked manually by the user.
- Use previously confirmed patterns without allowing them to override explicit contradictory evidence in the chosen date authority.
- Show the organized dates and a short explanation, such as **Matched previously confirmed pattern** or **Year inferred from payroll period**. No numeric confidence score is required.
- Ambiguity, an invalid date or an out-of-period date requires correction/resolution; no payroll write is allowed on guessed unresolved meaning.

Exact accepted text grammars, locale/normalization rules and pattern persistence need implementation design. This contract does not require a probabilistic model or unrestricted language interpretation.

### 7.3 Normal repeat case

For a recognized, unchanged source with compatible saved rules, the only normal confirmation should be the organized list of days. Relevant changes, identity problems, new categories, invalid values and payroll conflicts still require intervention. The reuse promise does not bypass validation on actual new values.

## 8. Quantities, ordinal inputs and domain limits

- Per-unit input ordinarily corresponds to one source quantity column, or to a valid explicitly configured numeric sum where supported.
- An ordinal pay item remains one input concept even if a transformation uses several source columns. The target receives a resulting quantity, not a new ordinal model or source-level pay calculation.
- The hypothetical ordinal prefix-column mode with binary `1, 1, 0` values and a count in the final open-ended bucket is **not a V1 requirement**. Do not build that specialized mode merely because the hypothetical example was discussed.
- Repeated-row quantities are combined first; apply the current ordinal calculation once to the final Driver/day/item quantity. Import does not calculate money itself.
- Ordinal results must be nonnegative whole numbers. Existing domain rules control topology, precision, supported methods and units.
- Keep the current ordinal topology/calculation rules unchanged. The existing domain has no six-tier cap, and this C2 delivery introduces none. Any new tier-count ceiling is separate domain work; neither the earlier hypothetical example nor configurable Import limits authorize it.
- Where payroll already normalizes supported formats/units, reuse that same authority. Import must not invent separate hour, distance or quantity semantics. An unsupported format is not made supported merely by this principle.
- Initial centrally configurable product allowances are **50 currently usable Company pay items** and **15 Branch payroll input concepts, including Status**. Status consumes one slot even though it is a separate domain. These are broader product/domain rules, not just Import DTO limits; raising them changes policy rather than fixed schema/input fields.
- Count currently usable concepts toward the Company/Branch limits. Retiring a concept frees its slot; historical definitions and frozen period snapshots remain preserved. Enforce capacity through canonical create/approval/activation/retirement authorities under compatible locks, including future effective configurations. Recording this rule does not alter existing domain data or historical snapshots.

## 9. Missing data, correction, confirmation and recovery

Distinguish missing files, missing driver rows, absent known columns, blank values, explicit zeros and deliberately filtered rows. One received input type does not establish that all inputs for that day arrived.

Missing coverage is a **warning**, not a blocker to payroll completion. A driver may have been absent or retained on the roster without work. Payroll Review owns the broader completeness assessment.

An import may add a value or change an existing value upward or downward after confirmation. Missing data is never a deletion instruction: if Ahmed's previously imported 100 miles is absent from the next file, keep the existing 100. Blank remains no supplied value; zero remains explicit zero under canonical payroll behavior.

Same actual completed data uploaded again is a no-op, including when someone manually changed payroll afterward. Preserve that later edit/provenance and explain prior completion without restoring the old file's value. A genuinely changed input must explain that data changed and request confirmation; a different filename, presentation or upload event alone is not a new payroll fact. Changed inputs/current payroll conflicts and stale previews still need the existing safeguards. There is no additive source-contribution ledger in V1.

Rejected/invalid batches retain mapping, filters, confirmed/pending setup and useful error state so the user can correct and continue. Block payroll writes until the complete batch is valid and confirmed. This does not replace atomic rejection of an unsafe file or require preserving malicious/unreadable content, and does not override finalization retention.

Original files need not remain forever. Saved mappings must survive original-file deletion and the existing permitted retention lifecycle. No purge rule may erase the durable setup needed for a later recognized import.

## 10. Separate Status import

Status has its own Import, using a file dedicated to Status. Do not mix imported Flussra Status values with ordinary work quantities in the same file. Its Status column is text, not a numeric quantity.

Detailed Status matching, allowed values, date/coverage rules, conflict behavior and delivery scope will be designed separately. Counting Status within the 15-concept Branch limit does not authorize its implementation now. Existing handling of work conflicting with a held full-day-off Status remains a separate payroll safeguard.

## 11. Revised user flow and preserved engineering boundaries

1. Upload the files for one source setup.
2. Perform bounded safety/canonical extraction, enforce per-file size and supported table/header rules, and validate header compatibility/union capacity. Explain rejected layouts and disclosed omissions.
3. Reuse compatible confirmed relationships or map the supplied payroll inputs. Resolve duplicate headers, selected sums and affected changed relationships.
4. Match drivers. Internally establish the chosen date context before deciding which rows are true duplicates; expose only the identity/aggregation questions that need a user decision.
5. Let the user choose the date authority, present interpreted dates/explanations, and resolve genuinely unclear dates. The screen sequence does not require duplicate detection to run before dates are known.
6. Apply the confirmed filters/transformations, normalize/validate quantities through payroll authority, aggregate to Driver/day/item, and compare the resulting supplied facts against current payroll.
7. Show missing-coverage warnings and required identity/category/value/conflict decisions. Obtain the required final confirmation.
8. Revalidate and apply the reviewed changes atomically through canonical payroll mutation/evidence. Activate replacement saved relationships only after successful completion. Keep the receipt/history and retention guarantees.

In the normal repeat case the saved interpretation reduces this to validation, the organized-day confirmation, and application. Identical completed data remains a no-op. The companion plan defines **twelve sequential Slice A stages, A01-A12**, with scope, dependencies and verification gates; these do not rename historical A1/A2/A3 work. A12 is the connected setup/read-only preview UI. Slice B remains separate for final identity/conflict/exception decisions, atomic application, successful rule promotion and receipt/history. Draft mapping/date/filter configuration is preparation, not permission to apply payroll. Existing deployment-budget acceptance remains open.

## 12. Preservation review and differences from the baseline

No implementation work is deleted by this contract. Existing assets can be reused unchanged, adapted or left unused by the eventual narrower product path. Preserve canonical payroll facts/evidence and useful security, staging, normalization, preview, retention and bulk-mutation boundaries. Pre-production status does not authorize rewriting Git history, weakening invariants or fabricating backward-compatibility requirements.

| Area | Preserved baseline at 9e03a66 | Revised product contract |
|---|---|---|
| Setup scope | Many heterogeneous source-layout groups in one batch | One source setup; compatible/partially overlapping header sets only |
| Worksheets/header | Multiple selected worksheets; header detection/selection | One sheet/file, one table, first-row header; no merged table cells |
| Capacity | Configurable 128 columns/unit and aggregate operational defaults | Configurable initial allowances of 40 combined columns, 10 MB uploaded and 1 MB canonical headed data/file; duplicates within a file count individually, corresponding daily columns count once; aggregate ceilings pending measurements |
| Presentation | Reader retains interpreted values/types under workbook safety rules | Presentation excluded from canonical byte size and data-column extent; formula information and cached results retained |
| Mapping | Per-import layout mappings; automatic cross-import reuse deferred | Branch-specific reusable per-target relationships and transformations; latest successfully completed replacement becomes current |
| Changed structure | Regroup/invalidate layout and preview | Preserve absent known-column relationships; remap affected changed relationships, including internal display-name changes |
| Quantity mapping | One source column per target; duplicate target assignment rejected | Direct input or explicit eligible Sum; no reuse of quantity occurrences |
| Duplicate headers | Ambiguous auto-suggestions; no multi-column target sum | Explicit per-occurrence resolution and numeric sum/ignore choices; changed duplicate arrangement invalidates that rule |
| Duplicate driver/day facts | Block all duplicate target facts across sources | User-confirmed within-day row aggregation, optionally categorical inclusion/exclusion |
| Driver discrepancy | Per-import stable-key/name resolution and blocking disagreements | Code authority with user acceptance for name discrepancies; unknown codes cannot be repaired by name |
| Dates | Column/file/sheet/fixed context; accepted candidates and numeric date order | Two user-facing authorities; contextual year/pattern interpretation and explanations; no missing-day guesses or numeric confidence |
| Coverage/corrections | Partial supplied sets, blank/zero distinction, omitted facts untouched | Missing coverage warns; incomplete sums produce no partial replacement; completed repeats preserve subsequent manual edits |
| Ordinal | Existing progressive domain calculation | Reuse domain after final quantity aggregation; hypothetical binary-prefix reader omitted |
| Status | Separate domain; Status import deferred | Dedicated text-only Status import required as a separate later design |
| Payroll domain capacity | Dynamic frozen definitions; benchmark counts were not domain limits | Configurable Company/Branch allowances count currently usable concepts; retirement frees slots, effective-date/concurrency guards preserve history; ordinal domain unchanged |
| Apply/security | Canonical internal bulk boundary exists; no public decision/apply flow | Preserve canonical authority, isolation, staleness, evidence and atomicity; public flow still needs implementation |

### 12.1 Pending behavior: completed-import recognition/notice

Reference preview implementation `6498453` contains `completed_notice` and a dedicated byte/semantic-match test. The foundation continuation has no equivalent completed-import notice lookup/field. It retains transport creation idempotency and semantic hashes; these are different protections from detecting a completed business import.

**Disposition: pending implementation/preservation review in A10; no implementation now.** Recognize complete interpreted mapped input within the same Company/Branch/period/targets and accepted-rule meaning, independently of presentation/ignored data. Identical completed data is a no-op preserving later manual edits, with completion/actor/time explanation. Preserve useful test intent without blindly restoring the old single-file API or treating a byte match/equal totals as sufficient permission to bypass validation.

Reference locations: `backend/app/imports/preview_service.py` (`completed_notice`, `enrich_metadata`) and `backend/tests/test_import_preview_api.py` (`test_completed_import_notice_uses_fixture_state_and_new_import_still_previews_current_facts`) on `6498453`. Foundation counterparts for review are `backend/app/imports/batch_service.py` metadata and semantic identity/publication boundaries.

### 12.2 Implemented behavior: paginated-read contract-version validation

The foundation continuation now routes metadata, mutations and all rows/issues/source-record pages through the shared supported-contract checker. Editable previews also compare their stored pending-rule context with the current target heads. Completed and cancelled history retains its original page reads after later successful head changes, while its own stored contract versions and revision/fingerprint remain checked.

**Disposition: implemented and focused-verified in A03.** The fail-closed supported-version guarantee applies consistently to metadata, every retained rows/issues/source-record page and mutations alongside tenant/revision/fingerprint protection. A10 still owns the separately pending completed-import recognition/notice behavior in §12.1. Do not add obsolete-version adapters or preserve an old helper/API solely for compatibility.

## 13. Open decisions and implementation acceptance prerequisites

The product choices are settled; the technical design and stage gates own their engineering delivery. The remaining evidence/deferred-design prerequisites are:

1. Final aggregate files/physical columns/rows/cells/canonical batch bytes and operational admission bounds after realistic measurements; keep applicable existing safeguards meanwhile.
2. Authoritative deployment request/proxy deadline and process memory/concurrency budget for A11 acceptance. Existing results do not certify new-cap performance or authorize jobs/polling.
3. Detailed dedicated Status import and any new ordinal tier-count ceiling remain separately deferred domain work.
4. Stage-local versioned schemas/DTOs, categorical suitability/date grammar coverage and remembered identity-acceptance details must implement the boundaries and tests in the design without inventing Company-wide reuse or new semantic equivalence. Escalate a genuinely new product ambiguity rather than silently changing this contract.
5. Completed-import recognition/notice in §12.1 remains pending A10 implementation/preservation review. Paginated-read contract-version validation in §12.2 was implemented and focused-verified in A03.

The existing implementation and historical test/benchmark evidence remain preserved. They do not prove conformance to the complete revised contract or mark A04-A12/Slice B accepted. A03's schema and runtime changes passed its focused verification and SQL/Alembic authority comparison; later stages still require separate execution authorization and fresh focused correctness, retention/concurrency and realistic workload verification.
