# Flussra C2 Import Product Contract

**Status:** Agreed product decisions, 2026-10-26. This is the canonical contract for the revised Import behavior. Implementation reconciliation, migration/refactor design and execution are not approved by this document.

**Continuation baseline:** `codex/c2-import-foundation` at `9e03a660b2fa06c4e2aa498715e4cacee2773f6e`. Preserve its implementation and history. The divergent `codex/c2-import-preview` at `6498453` is reference evidence only; it is not to be merged or cherry-picked as part of recording this contract.

**Authority:** This document supersedes conflicting product decisions in the [existing C2 product plan](FLUSSRA_C2_IMPORT_PLAN.md) and [existing implementation design](FLUSSRA_C2_IMPORT_DESIGN.md). Those documents remain the record of the preserved architecture, implemented behavior, measurements and compatible safeguards. A rule recorded here is not a claim that it is implemented. Open decisions below have no invented defaults. No implementation, schema or deployed behavior changes with publication of this contract.

## 1. Purpose and boundaries

Import supplies work facts to an existing editable payroll period. The period's frozen definitions, driver/date eligibility and existing payroll domain remain authoritative. Import prepares quantities; the payroll engine calculates money. Import does not create master data, invent a new ordinal model or create a parallel payroll writer.

One period can receive multiple partial imports. One successful import does not have to provide every payroll input or complete every day. Imported facts remain subject to existing authorization, Company/Branch isolation, eligibility, conflict review, atomic application, commit-time revalidation and audit/evidence requirements.

Status belongs to its own domain. Ordinary work import and Status import are separate contracts and separate files (§10).

## 2. One source setup and supported file shape

### 2.1 Meaning of a source setup

- Understand and finish **one source per setup**. A different source gets a separate setup afterward. A setup may include multiple files representing that source's days.
- A source is the user-confirmed input structure and its mapping rules. This contract does not require a new business catalog of named source systems or automatic discovery of relationships between different files.
- Source columns represent payroll inputs and identity/date/filter/reference information. Columns do not represent days or months. `Driver | Monday | Tuesday | Wednesday` is unsupported.
- Date authority is chosen separately: file/sheet name or a Date Column (§7).
- Reject an upload/setup mixing unsupported day-column layouts with supported files. Explain the accepted shape, that other payroll inputs can be added through a later setup, and that confirmed compatible setup can be reused.

### 2.2 Tabular restrictions

V1 accepts normal, organized `.xlsx` or `.csv` tables:

1. One sheet per file; no multiple-sheet workbook input.
2. One table per sheet.
3. The first row is the header.
4. No merged cells inside the imported table.
5. No arbitrary report layouts or multiple tables.

Presentation-only cells do not extend the import table. Formatting, styles, fonts, colors and data validations in empty rows/columns do not make them data.

Data beneath an absent header is ignored automatically, without requiring a decision for each such column. After inspection, show a clear notice, for example: **Some data was ignored because no column header was provided. Please review your file.** This is disclosed omission, not hidden data loss. How those populated headerless columns affect the column ceiling remains open (§13).

Populated hidden-column treatment remains open. A possible future color/style-based Import type requires separate study and is outside this contract.

### 2.3 Header compatibility and the 40-column limit

Initial structure comparison examines **column names/headers**, not column contents. Do not inspect sample payroll values to decide initial header similarity.

- Repeated daily structures count once: `Driver | Code | A | B` in six files is four unique columns, not twenty-four.
- Partially overlapping structures may participate in the same source setup. `Driver | Code | A | B` and `Driver | Code | A | C` have the union `Driver | Code | A | B | C`: five unique columns.
- Files with completely different column sets are rejected from the same setup.
- The header union must contain at most **40 unique columns**, including identity, name, code, date, filter and other included columns, not merely pay-item quantities.
- Reject an incompatible or over-limit setup as a whole; do not silently truncate or partially accept it.
- Count the imported header structure during bounded inspection, before payroll interpretation. Format-only empty columns do not count.

Header normalization and the interaction of duplicate-header multiplicity with the 40-column ceiling are not yet settled. Separate row/cell/batch ceilings must account for physical input size even when labels repeat.

## 3. Upload, canonical data and formulas

### 3.1 Two per-file limits

| Boundary | Maximum |
|---|---:|
| Uploaded file | 10 MB per file |
| Extracted canonical import data | 1 MB per file |

The repository's existing decimal byte convention applies: 1 MB = 1,000,000 bytes. The upload ceiling is enforced before extraction. Extract a canonical import representation and measure it before the actual mapping/payroll interpretation proceeds. Do not generate a cleaned Excel workbook merely to measure its compressed size.

Canonical import data includes the retained headers, cell values, formulas, cached formula results and types needed for interpretation. Exclude presentation-only colors, fonts, borders, styles, formatting, data validations and other unnecessary presentation metadata. Preserve interpretation meaning derived from the original workbook, including date/time/duration types; discarding display styles must not discard semantic type information.

Canonical data at or below 1 MB is eligible for further processing; above 1 MB is rejected. Extraction is itself bounded parsing work, not a cost-free operation. Retain applicable file-content, archive/decompression, XML, numeric-precision and resource protections while extracting. This product revision does not authorize removing security checks or making any batch unbounded.

The deterministic canonical representation, its encoding/measurement details and treatment of ignored data must be specified before implementation. No serialized format or new batch maximum is selected here.

### 3.2 Formula results

- Preserve formula information and its final saved/cached calculated result in canonical import data where applicable.
- Import uses the saved/cached result. Flussra does not evaluate or recalculate spreadsheet formulas.
- A formula needed for an imported value without a saved/cached result is a validation error; that value cannot be accepted as blank or zero.
- Validate the cached value through the same target-domain rules as an ordinary value.

### 3.3 Batch resource policy

Final ceilings for total files, rows, cells and canonical/processed batch bytes remain **open until realistic workloads are benchmarked**. The 1 MB per-file policy does not establish safe aggregate cost. Existing configured safety ceilings remain implementation safeguards, not newly approved final product limits. No performance guarantee or asynchronous execution requirement is inferred from the new per-file limit.

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

- An absent known column means this upload did not supply that input. Its saved relationship remains valid; missing coverage may warn.
- A changed external name, such as `Miles` becoming `Total Distance`, must not be assumed equivalent. Remap only the affected relationship.
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
- A quantity input column occurrence already consumed by one quantity mapping cannot be reused elsewhere in that setup. Shared Driver, Date and Filter columns may be reused by multiple mappings.
- The final sum must obey the target's canonical quantity validation. An ordinal result must be a whole number greater than or equal to zero.

Identifying duplicate occurrences and their arrangement is part of saved rule identity. The precise normalization/position-compatibility contract is still an implementation-design decision; do not silently make duplicate occurrences interchangeable.

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
- The originally stated six-level ceiling belongs to the ordinal/product domain, not an import-specific reinterpretation. Do not invent a new topology or infer source column capacity from six levels; any discrepancy with current domain enforcement needs domain reconciliation before implementation.
- Where payroll already normalizes supported formats/units, reuse that same authority. Import must not invent separate hour, distance or quantity semantics. An unsupported format is not made supported merely by this principle.
- A Company may create up to **50 pay items**. A Branch may use at most **15 payroll input concepts, including Status**. Status consumes one slot even though it is a separate domain. These are broader product/domain rules, not just Import DTO limits.
- Whether retired concepts release slots, and the applicable governance/effective-period enforcement, need explicit domain decisions. Existing snapshots and domain data are not modified by recording these limits.

## 9. Missing data, correction, confirmation and recovery

Distinguish missing files, missing driver rows, absent known columns, blank values, explicit zeros and deliberately filtered rows. One received input type does not establish that all inputs for that day arrived.

Missing coverage is a **warning**, not a blocker to payroll completion. A driver may have been absent or retained on the roster without work. Payroll Review owns the broader completeness assessment.

An import may add a value or change an existing value upward or downward after confirmation. Missing data is never a deletion instruction: if Ahmed's previously imported 100 miles is absent from the next file, keep the existing 100. Blank remains no supplied value; zero remains explicit zero under canonical payroll behavior.

Same actual data uploaded again is a no-op. A genuinely changed input must explain that data changed and request confirmation; a different filename, presentation or upload event alone is not a new payroll fact. Current payroll conflicts and stale previews still need the existing safeguards. There is no additive source-contribution ledger in V1.

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

In the normal repeat case the saved interpretation reduces this to validation, the organized-day confirmation, and application. This is a product flow, not an approved endpoint/schema/migration sequence. A3/public apply remains unimplemented, and existing deployment-budget acceptance remains open.

## 12. Preservation review and differences from the baseline

No implementation work is deleted by this contract. Existing assets can be reused unchanged, adapted or left unused by the eventual narrower product path. Preserve canonical payroll facts/evidence and useful security, staging, normalization, preview, retention and bulk-mutation boundaries. Pre-production status does not authorize rewriting Git history, weakening invariants or fabricating backward-compatibility requirements.

| Area | Preserved baseline at 9e03a66 | Revised product contract |
|---|---|---|
| Setup scope | Many heterogeneous source-layout groups in one batch | One source setup; compatible/partially overlapping header sets only |
| Worksheets/header | Multiple selected worksheets; header detection/selection | One sheet/file, one table, first-row header; no merged table cells |
| Capacity | Configurable 128 columns/unit and aggregate operational defaults | 40 unique headers/setup; 10 MB uploaded and 1 MB canonical data/file; final aggregate ceilings pending measurements |
| Presentation | Reader retains interpreted values/types under workbook safety rules | Presentation excluded from canonical byte size and data-column extent; formula information and cached results retained |
| Mapping | Per-import layout mappings; automatic cross-import reuse deferred | Per-target reusable relationships and transformations; latest successfully completed replacement becomes current |
| Changed structure | Regroup/invalidate layout and preview | Preserve absent known-column relationships; remap affected changed relationships, including internal display-name changes |
| Quantity mapping | One source column per target; duplicate target assignment rejected | Direct input or explicit eligible Sum; no reuse of quantity occurrences |
| Duplicate headers | Ambiguous auto-suggestions; no multi-column target sum | Explicit per-occurrence resolution and numeric sum/ignore choices; changed duplicate arrangement invalidates that rule |
| Duplicate driver/day facts | Block all duplicate target facts across sources | User-confirmed within-day row aggregation, optionally categorical inclusion/exclusion |
| Driver discrepancy | Per-import stable-key/name resolution and blocking disagreements | Code authority with user acceptance for name discrepancies; unknown codes cannot be repaired by name |
| Dates | Column/file/sheet/fixed context; accepted candidates and numeric date order | Two user-facing authorities; contextual year/pattern interpretation and explanations; no missing-day guesses or numeric confidence |
| Coverage/corrections | Partial supplied sets, blank/zero distinction, omitted facts untouched | Preserve those guarantees and explicitly classify missing coverage as warning-only |
| Ordinal | Existing progressive domain calculation | Reuse domain after final quantity aggregation; hypothetical binary-prefix reader omitted |
| Status | Separate domain; Status import deferred | Dedicated text-only Status import required as a separate later design |
| Payroll domain capacity | Dynamic frozen definitions; benchmark counts were not domain limits | New Company/Branch product ceilings; retirement/governance enforcement unresolved |
| Apply/security | Canonical internal bulk boundary exists; no public decision/apply flow | Preserve canonical authority, isolation, staleness, evidence and atomicity; public flow still needs implementation |

### 12.1 Pending behavior: completed-import recognition/notice

Reference preview implementation `6498453` contains `completed_notice` and a dedicated byte/semantic-match test. The foundation continuation has no equivalent completed-import notice lookup/field. It retains transport creation idempotency and semantic hashes; these are different protections from detecting a completed business import.

**Disposition: pending preservation review; no implementation now.** Revised repeat behavior still requires a no-op for identical actual data. Determine recognition scope, saved mapping version, period, canonical/ignored-data meaning, current-payroll conflicts and the suitable receipt/day-review message under this contract. Preserve the useful behavior and test intent; do not blindly restore the old single-file API or treat a byte match as permission to suppress necessary validation.

Reference locations: `backend/app/imports/preview_service.py` (`completed_notice`, `enrich_metadata`) and `backend/tests/test_import_preview_api.py` (`test_completed_import_notice_uses_fixture_state_and_new_import_still_previews_current_facts`) on `6498453`. Foundation counterparts for review are `backend/app/imports/batch_service.py` metadata and semantic identity/publication boundaries.

### 12.2 Pending behavior: paginated-read contract-version validation

The reference preview validates supported contract versions before paginated rows/issues reads. Foundation's full metadata loader validates versions, but `backend/app/imports/readmodels.py`'s shared `_read_identity` helper checks scope/revision/fingerprint without repeating those contract-version checks for rows/issues/source-record reads.

**Disposition: pending preservation review; no implementation now.** The underlying fail-closed supported-version guarantee remains compatible with the revised contract. Reconcile the version authority and enforcement boundary for every retained paginated read after the new source/mapping/canonical representation is designed. Do not add obsolete-version adapters or preserve an old helper/API solely for compatibility. Keep version-mismatch rejection and tenant/revision protection test intent visible in that review.

## 13. Open decisions and implementation acceptance prerequisites

These points are explicitly not resolved by this document:

1. Header normalization: case, whitespace, Unicode and other equivalence rules.
2. Populated hidden columns; whether ignored populated headerless columns and duplicate occurrences count toward the 40-column ceiling.
3. Deterministic canonical data representation/encoding, exact byte-accounting boundary and treatment of ignored content/formula information.
4. Total files/rows/cells/canonical batch bytes and any revised operational admission bounds, after realistic measurements. Retain existing applicable safeguards meanwhile.
5. Company/Branch active-versus-retired counting, effective-period governance and any reconciliation of the stated ordinal ceiling with existing domain enforcement.
6. Detailed Status import contract and its separately approved implementation scope.
7. Categorical suitability checks, duplicate-occurrence compatibility, date text grammars/locales and saved-pattern matching details.
8. Detailed persistence/activation/ownership of reusable per-target rules, the scope of remembered identity acceptances, and their interaction with period snapshots and retention. Do not invent a source-system business catalog or additive contribution model.
9. Revised content/repeat identity and consistent supported-version enforcement, including the two preservation-review behaviors in §12.
10. Deployment request/proxy deadline, memory/concurrency budget and revised workload acceptance. Existing measurements do not prove new-cap performance; no worker/job architecture or arbitrary latency target is approved.

The existing implementation and recorded test/benchmark evidence remain preserved at the continuation baseline. They do not prove conformance to this new contract. Future implementation requires an explicit reconciliation plan and authorization, followed by focused correctness, schema/retention/concurrency and realistic workload verification of the changed behavior. Recording these decisions executes none of that work.
