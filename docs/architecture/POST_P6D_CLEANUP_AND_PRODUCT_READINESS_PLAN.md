# Flussra Post-P6D Cleanup and Product Readiness Plan

**Status:** Active execution roadmap after P6D
**Current baseline:** P6D closed in `04b0a4c` (`feat: add finalized payroll audit evidence`) and documented in `d9df046` (`docs: close p6d finalized audit`); Alembic `0065` is the current head.
**Current stage:** Stage A discovery is complete; Stage B contract and authority alignment is next.

## Purpose and authority

This plan begins after the formal closure of P6D. It governs the temporary Cleanup and Product Readiness direction while the remaining Phase 6 and Phase 7 implementation units are frozen.

The [Current Payroll Backend Master Plan](CURRENT_PAYROLL_BACKEND_MASTER_PLAN.md) remains the architectural and historical reference for Current Payroll business rules and decisions already made. Source code, migrations, executable tests, and the current schema remain authoritative for implemented reality. This plan controls post-P6D work priority and product-readiness sequencing; it does not silently replace existing Current Payroll business or architecture rules. Any future change to an established rule must be made explicitly.

## Why execution is changing

The Current Payroll backend has reached substantial depth in lifecycle handling, immutable calculation authority, reporting, finalized-library reads, and finalized audit history. Continuing immediately into P6E, P6F, or Phase 7 would deepen backend work before resolving product-readiness concerns.

The current priority is to:

1. Understand and reduce accumulated code and legacy complexity.
2. Make implemented capability easier to maintain and reason about.
3. Expose and use existing backend capability where it is useful to the product.
4. Solve practical inbound and outbound payroll data movement.
5. Create a pilot-ready end-to-end product.
6. Validate real-world value before resuming broader backend hardening or expansion.

This does not characterize prior work as wasted or the backend as complete. It changes execution order so product evidence can guide the next investment.

## Deferred backend roadmap

P6D is the stopping boundary of the prior execution sequence. P6E, P6F, and Phase 7 remain defined, deferred, and frozen rather than cancelled. Their architecture and historical context remain in the Current Payroll Backend Master Plan.

The freeze does not prevent an immediately necessary, separately scoped P0/P1 fix for payroll correctness, financial authority, company/branch/tenant isolation, security, immutable finalized history, or data corruption.

## Stage A - Cleanup Discovery - Complete

**Objective:** Determine what actually exists in the repository before deleting, moving, or rewriting anything.

Stage A was completed as a read-only repository audit. It covered backend, frontend, tests, database and runtime-schema references, API contracts, compatibility paths, dormant structures, and legacy code. It performed no deletion or refactor.

The discovery found that the main cleanup risk is not isolated dead code. Multiple generations of backend contracts and frontend consumers coexist, and some older contracts remain active because the frontend or compatibility behavior still depends on them. Stage B must therefore establish canonical authority and migrate active consumers before removing superseded contracts.

### Classification model

| Classification | Meaning |
| --- | --- |
| `ACTIVE` | Current runtime or product path. |
| `SAFE DEAD` | Strong evidence shows no runtime, public-contract, historical, migration, test, or compatibility requirement remains. |
| `LEGACY BUT REQUIRED` | Old representation or path retained for historical data or compatibility. |
| `OBSOLETE PUBLIC CONTRACT` | Superseded contract requiring explicit deprecation rather than immediate deletion. |
| `DORMANT / SOURCE-OF-TRUTH RISK` | Dormant or competing implementation that obscures canonical ownership. |
| `TEST-ONLY` | Intentionally supports testing without production runtime behavior. |
| `FUTURE / RESERVED` | Speculative or future structure that is not active. |
| `UNCERTAIN` | Insufficient evidence for safe removal. |

`UNCERTAIN` code must not be removed merely because static search finds no obvious caller.

### Required outputs

1. Repository and domain reality map.
2. Dead and legacy inventory.
3. Duplicate and source-of-truth ambiguity inventory.
4. Current, legacy, and deprecated API inventory.
5. Architectural hotspot inventory.
6. Proposed safe cleanup order.
7. Explicit evidence and confidence for every proposed deletion.

The audit should independently revalidate high-responsibility modules, including `backend/app/payroll/service.py`. File size alone is not evidence of poor code or a reason to split a module.

## Stage B - Contract Alignment, Cleanup, and Structural Refactor

Stage B begins only after Stage A findings are reviewed and approved. Its sequence is evidence-driven.

The governing sequence is:

```text
identify canonical authority
-> migrate active consumers to it
-> verify behavior
-> deprecate/remove superseded contracts
-> clean stale tests/dead code
-> structurally refactor the remaining active implementation
```

### B1 - Contract and authority alignment

Use the Stage A findings to establish the canonical backend contract and source for each active product capability. Current source, schema, migrations, and executable behavior define implemented reality; the Current Payroll Backend Master Plan records why the completed backend capabilities were intentionally built.

Where the frontend uses an older or superseded contract, migrate that active consumer to the intended authoritative backend contract and verify behavior before considering removal of the old path. Where a newer backend capability has no frontend representation, decide whether it is required for the usable Current Payroll product before exposing it. Do not build UI merely because an endpoint exists.

The alignment map must cover, where supported by current source reality:

- Current Payroll context and Hub;
- workflow and candidate-period behavior;
- Daily Grid and operational source entry;
- Status and Bonus;
- calculation preview and Working Drivers;
- Off Drivers and Review;
- CP-5C reports;
- finalized overview and reports;
- finalized Off/Status;
- finalized rates/rules/Bonus-used evidence;
- finalized audit.

For each capability, classify whether the frontend already uses the authoritative contract, uses an older contract, partially uses the newer capability, does not expose it, or does not need it for the target workflow. This map drives migration and retention decisions.

### B2 - Contract decommissioning

After consumers have been migrated and behavior verified, evaluate superseded contracts for deprecation or removal. Account for remaining frontend and backend consumers, known external/public consumers, historical-data compatibility, migrations and persisted values, and tests that protect genuinely supported behavior.

For competing or dormant structures, explicitly choose one of: delete, deprecate, compatibility wrapper, historical/read-only support, or retain until a later migration. A legacy path is removable only after this evidence is complete and canonical implementation ownership is clear.

### B3 - Test and dead-code cleanup

Once a behavior or contract is intentionally retired, remove or modernize tests that only enforce it. Correct stale tests, fixtures, comments, docstrings, and OpenAPI descriptions that contradict current behavior; remove proven dead helpers, wrappers, components, styles, schemas, and other unreachable code; and preserve characterization and history tests where they still protect real compatibility or immutable history. Tests must not be weakened merely to simplify cleanup.

### B4 - Structural refactor

Only after contract consolidation and dead-code reduction have simplified the active surface should active modules be extracted incrementally along domain boundaries found by the audit. Do not perform a full rewrite or split modules by arbitrary line count. Possible boundaries, if supported by dependency evidence, include period lifecycle and creation, source entry and Daily Grid, eligibility, Status, Bonus and period pay, calculation, submission and snapshots, finalization, rates and rules, and compatibility support. Each extraction must preserve behavior and remain independently reviewable.

### Advanced calculation methods

Existing advanced calculation implementations, including `OrdinalTier`, `RangeBracket`, `RangeProgressive`, and `Block`, are intentionally retained and are outside cleanup removal or redesign scope. Primary product exposure may currently use PerUnit, but lack of primary-UI exposure is not evidence that these implementations are dead.

A later explicit product and architecture decision may continue, revise, replace, or remove some or all of these methods after a replacement exists. Stage B must not delete or redesign them opportunistically.

### Cleanup invariant

Stages A and B are not a product redesign. They must not change payroll formulas, lifecycle rules, permissions, or historical authority, and must not add features merely because a file is being touched. A discovered P0/P1 is handled as a separately scoped blocker; other product questions are recorded for later.

## Stage C - Product Readiness and Pilot

**Objective:** Determine the minimum additional work needed to put Flussra in front of a real payroll user and evaluate whether it provides meaningful value.

Stage C begins after contract consolidation and core frontend/backend alignment. Import, export, external-integration strategy, and pilot validation remain Product Readiness work rather than contract-cleanup work.

### C1 - Pilot-ready Current Payroll

Build a coherent end-to-end Current Payroll experience by reusing existing authoritative backend behavior where possible. The required slice must follow source reality, but likely spans period workflow; Open, Prepared, InReview, and Returned visibility; operational entry and correction; Status; Bonus; expected payroll; alerts; Submit; Review; Return/Correction/Resubmit; Approval; Finalization; required reports; and finalized result.

The frontend must not recalculate official financial truth.

### C2 - Inbound data and import

Validate CSV/Excel import before speculative direct external APIs. Import is a workflow, not an upload button. Its architecture plan must cover file and sheet selection, header detection, column mapping, driver and PayItem mapping, date/period/branch validation, data types and units, duplicate detection, preview, valid/warning/rejected rows, precise rejection reasons, transactions, idempotency, history, reusable mapping profiles, provenance/audit, and retry/correction behavior.

The importer feeds Flussra's canonical operational source model. It must not become a competing payroll source of truth.

### C3 - Outbound data and export

Start with generic CSV/Excel export from authoritative finalized payroll data. Use reusable Export Profiles so future formats can target payroll, accounting, or HCM systems. Export adapters transform finalized results and do not recalculate payroll.

### C4 - Integration seam

Prepare inbound and outbound adapter seams, but do not build direct vendor integrations without customer evidence. Potential inbound categories include ELD, TMS, fleet, and time/attendance systems; potential outbound categories include payroll providers, accounting systems, and HCM systems. Specific vendors are not roadmap commitments. The first direct integration follows pilot or customer demand.

### C5 - Pilot

Define the minimum pilot after the gap audit. A working hypothesis is:

```text
Company / Branch
-> Drivers
-> Pay Items / Rates
-> Payroll Period
-> Import operational data
-> Daily corrections/exceptions
-> Status / Bonus
-> Expected payroll
-> Submit
-> Review
-> Return/Correction if needed
-> Approve
-> Finalize
-> Reports
-> Export
```

This is not a locked product contract. Remove unnecessary steps when evidence shows they are not needed.

## Product validation and decision gates

Evaluate product readiness independently of code quality. Measure payroll preparation time, manual entry, detected and prevented errors, correction effort, review effort, explainability of a driver's pay, reconciliation of final totals, external handoff/export difficulty, and whether users want to use Flussra again for the next payroll period.

| Decision | Gate |
| --- | --- |
| `GO` | Continue deeper investment when real pilot evidence shows meaningful workflow improvement and repeated-use demand. |
| `PIVOT` | Change scope, positioning, or integration priorities when the pain is real but the product shape is wrong. |
| `STOP` | Reevaluate or stop Flussra as a standalone SaaS when workflow improvement is marginal, manual work remains essentially unchanged, integration cost outweighs value, repeat use is absent, or adoption/willingness to pay is absent. Do not respond by adding arbitrary features. |

## Explicit non-goals

Until separately justified, do not prioritize Driver Self-Service, Allowance Tracking, Leave Management, Vehicle Inspections, broad Driver Operations expansion, speculative Calculation Methods, a formula DSL/plugin framework, random direct vendor integrations, self-service billing sophistication, marketing-site perfection, or backend architecture work justified only by theoretical enterprise completeness.

## Decision record

1. P6D is the current stopping boundary of the prior execution roadmap.
2. P6E, P6F, and Phase 7 are deferred, not cancelled.
3. Cleanup and canonical contract alignment precede new feature expansion.
4. Cleanup begins with read-only discovery, not deletion.
5. Canonical authority is identified and active consumers are migrated before superseded contracts are removed.
6. Stale tests and proven dead code are cleaned after intentional contract retirement and before structural refactoring.
7. Active modules are refactored incrementally along real domain boundaries after contract consolidation.
8. Existing backend capability is evaluated for product need and use before duplicate backend work or UI is created.
9. Existing advanced calculation methods remain outside cleanup removal pending a separate product and architecture decision.
10. Import and export are core Product Readiness concerns.
11. Direct vendor integrations follow customer evidence.
12. Real pilot evidence determines whether deeper investment continues.
13. After pilot evidence, the deferred backend roadmap is reconsidered.
