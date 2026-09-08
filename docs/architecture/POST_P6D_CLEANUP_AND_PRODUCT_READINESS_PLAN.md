# Flussra Post-P6D Cleanup and Product Readiness Plan

**Status:** Active execution roadmap after P6D
**Current baseline:** P6D closed in `04b0a4c` (`feat: add finalized payroll audit evidence`) and documented in `d9df046` (`docs: close p6d finalized audit`); Alembic `0065` is the current head.

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

## Stage A - Cleanup Discovery

**Objective:** Determine what actually exists in the repository before deleting, moving, or rewriting anything.

Stage A is read-only. It covers backend, frontend, tests, database and runtime-schema references, API contracts, compatibility paths, dormant structures, and legacy code. It performs no deletion or refactor.

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

## Stage B - Cleanup Execution and Structural Refactor

Stage B begins only after Stage A findings are reviewed and approved. Its sequence is evidence-driven.

### B1 - Remove proven dead code

Delete only work classified with sufficient evidence as safe to remove. Do not delete historical migration files merely because they no longer execute at runtime. Do not remove compatibility behavior without explicit evidence or decision.

### B2 - Resolve source-of-truth ambiguity

For competing or dormant structures, explicitly choose one of: delete, deprecate, compatibility wrapper, historical/read-only support, or retain until a later migration. The result must make canonical implementation ownership clear to a future developer.

### B3 - Modernize stale tests and contracts

After intentional behavior removal, remove tests that exclusively enforce obsolete behavior; modernize stale fixtures, comments, docstrings, and OpenAPI descriptions; and retain genuine business assertions. Tests must not be weakened merely to simplify refactoring.

### B4 - Structural refactor

Only after proven dead and obsolete code is reduced, extract active modules incrementally along domain boundaries found by the audit. Do not perform a full rewrite or split modules by arbitrary line count. Possible boundaries, if supported by dependency evidence, include period lifecycle and creation, source entry and Daily Grid, eligibility, Status, Bonus and period pay, calculation, submission and snapshots, finalization, rates and rules, and compatibility support. Each extraction must preserve behavior and remain independently reviewable.

### Cleanup invariant

Stages A and B are not a product redesign. They must not change payroll formulas, lifecycle rules, permissions, or historical authority, and must not add features merely because a file is being touched. A discovered P0/P1 is handled as a separately scoped blocker; other product questions are recorded for later.

## Stage C - Product Readiness and Pilot

**Objective:** Determine the minimum additional work needed to put Flussra in front of a real payroll user and evaluate whether it provides meaningful value.

### C1 - Existing backend to frontend catch-up

Before building substantial backend domains, audit which implemented backend capabilities are absent from or incorrectly represented by the frontend. Inspect at least:

- Current Payroll Hub and calculation preview;
- Working Drivers and Off Drivers;
- Bonus summary and batch behavior;
- review snapshot behavior;
- CP-5C calculation reports;
- finalized overview and reports;
- finalized Off/Status, Rates Used, and P6D audit.

Identify capabilities with no useful UI, legacy frontend endpoints superseded by authoritative contracts, frontend aggregation that should rely on backend authority, and capabilities that should remain unexposed for the pilot. Do not create UI solely because an endpoint exists.

### C2 - Pilot-ready Current Payroll

Build a coherent end-to-end Current Payroll experience by reusing existing authoritative backend behavior where possible. The required slice must follow source reality, but likely spans period workflow; Open, Prepared, InReview, and Returned visibility; operational entry and correction; Status; Bonus; expected payroll; alerts; Submit; Review; Return/Correction/Resubmit; Approval; Finalization; required reports; and finalized result.

The frontend must not recalculate official financial truth.

### C3 - Inbound data and import

Validate CSV/Excel import before speculative direct external APIs. Import is a workflow, not an upload button. Its architecture plan must cover file and sheet selection, header detection, column mapping, driver and PayItem mapping, date/period/branch validation, data types and units, duplicate detection, preview, valid/warning/rejected rows, precise rejection reasons, transactions, idempotency, history, reusable mapping profiles, provenance/audit, and retry/correction behavior.

The importer feeds Flussra's canonical operational source model. It must not become a competing payroll source of truth.

### C4 - Outbound data and export

Start with generic CSV/Excel export from authoritative finalized payroll data. Use reusable Export Profiles so future formats can target payroll, accounting, or HCM systems. Export adapters transform finalized results and do not recalculate payroll.

### C5 - Integration seam

Prepare inbound and outbound adapter seams, but do not build direct vendor integrations without customer evidence. Potential inbound categories include ELD, TMS, fleet, and time/attendance systems; potential outbound categories include payroll providers, accounting systems, and HCM systems. Specific vendors are not roadmap commitments. The first direct integration follows pilot or customer demand.

### C6 - Pilot

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
3. Cleanup precedes new feature expansion.
4. Cleanup begins with read-only discovery, not deletion.
5. Proven dead or obsolete code is removed before structural refactoring.
6. Active modules are refactored incrementally along real domain boundaries.
7. Existing backend capability is evaluated for use before duplicate backend work is created.
8. Import and export are core product-readiness concerns.
9. Direct vendor integrations follow customer evidence.
10. Real pilot evidence determines whether deeper investment continues.
11. After pilot evidence, the deferred backend roadmap is reconsidered.
