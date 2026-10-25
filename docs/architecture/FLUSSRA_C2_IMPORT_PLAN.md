# FLUSSRA C2 IMPORT PLAN

**Status:** Agreed product direction (2026-10-24). C2 is not started. This is not an implementation plan: the implementation design gets its own review and must satisfy the C2 gate in `FLUSSRA_UNIFIED_REFOUNDATION_EXECUTION_PLAN.md` §10.<br>
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
| R3 | Every imported value is traceable to its batch, file, sheet, row and column while its period can change; after finalization, to its durable import and audit record (D7). |
| R4 | The period is the import schema: not the current settings, not a fixed template. Importing into an older period uses that period's frozen Pay Items. |
| R5 | Import never calculates pay. It supplies driver, date, Pay Item and quantity; the calculation engine does the rest. |
| R6 | Import never creates master data. Unknown drivers or columns are reported, never created. |
| R7 | Pay Items are never imported. They are configured in Flussra only. A bulk Pay Item tool, if ever needed, is a separate configuration feature and not part of Import. |
| R8 | A Status in a file (for example PTO) belongs to the Status domain and never becomes a Pay Item. |
| R9 | Everything before confirmation is staging, not payroll truth. |
| R10 | All or nothing. There is no accidental partial import; the user may deliberately exclude rows. |
| R11 | Identifying a driver is separate from that driver being eligible for the period and date. Both are checked. |
| R12 | Blank is not zero. Blank means the file gave no value; zero means it gave zero. |
| R13 | Suggestions may assist mapping, but nothing decides payroll automatically. Anything ambiguous needs a person. |

## 3. Version 1 — included (S)

| ID | Included |
|---|---|
| S1 | `.xlsx` and `.csv` only, read by one engine. |
| S2 | Two ways in: upload your own file, or download this period's Flussra template. |
| S3 | Sheet selection and header-row detection. |
| S4 | Column mapping: exact header matches map automatically, the user maps the rest, and columns the period does not need are ignored. |
| S5 | Driver matching: stable driver identifier first; an exact name with a single candidate is a suggestion the user confirms; ambiguous names block until the user chooses. |
| S6 | Validation: value type and unit per Pay Item, date inside the period, driver eligible for that period and date. A normal day off is still a valid date. |
| S7 | Collisions: same value is unchanged; an empty cell is new; a different value is a conflict resolved as *keep existing* or *use imported*, with *apply to all similar*; the same cell twice in one file is a blocking error. |
| S8 | Precise, human error messages naming the row, field, value and reason. |
| S9 | Impact preview (new, changed, unchanged, skipped, conflicts resolved, errors) and a confirm button that states the number of changes. |
| S10 | Commit-time revalidation: the period is still editable and nothing reviewed has changed, otherwise the preview must be refreshed. |
| S11 | Re-uploading an already completed import changes nothing and says when and by whom it was done. |
| S12 | Atomic commit into the period's daily entries, marked as imported. |
| S13 | An import receipt and the period's import history: file, user, time, counts and the mapping used. |
| S14 | Value-level provenance from each imported value to its batch, file, row and column. |

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
| L3 | Saved external driver aliases (source system + external key + Driver; DWR-023) | The user resolves unmatched drivers in each import | Remember resolutions as aliases |
| L4 | Correcting values inside Flussra | Resolve conflicts, choose drivers and exclude rows in Flussra; wrong values are fixed in the file and re-uploaded | Inline cell editing in the review table |
| L5 | Template recognition and drift detection | The Flussra template carries a hidden period stamp so it maps automatically | Report which fields changed since the template was generated |

## 6. Placement

- One import belongs to exactly one period: one branch, one date range, one set of frozen Pay Items. Rows outside the period are flagged, never routed to another period. A file covering two periods is imported once in each.
- Import history is shown inside the period. A company-wide Import Hub follows only when there is something to manage across periods (saved profiles, aliases, People and Rate imports), and then as *Data Imports / Import History*.
- Onboarding is led by the Setup Center (Set up business → Set up payroll model → Run payroll). Import appears inside the step that needs it rather than as a destination of its own.

## 7. Version 1 flow (C)

```
C1  Open the period → Add payroll data
C2  Upload own file   or   download this period's template
C3  Choose sheet and header row
C4  Map columns: Driver, Date, and the period's Pay Items
C5  Match drivers: identifier → confirmed name → user choice
C6  Validate: type, unit, date, eligibility, in-file duplicates
C7  Review: valid, warnings, errors, decisions (conflicts, full-day-off Statuses, day-off work); decide or exclude
C8  Impact preview → Import N changes
C9  Revalidate at commit → commit everything at once
C10 Receipt → links to the Daily Grid and the updated payroll preview
```

Payroll is unchanged until C9.

Import lifecycle: `UPLOADED → ANALYZING → NEEDS_MAPPING → NEEDS_REVIEW → READY → COMMITTING → COMPLETED`, plus `FAILED` and `CANCELLED`.

## 8. Foundations required from the start (F)

| ID | Requirement | What it keeps open |
|---|---|---|
| F1 | One shared engine; each import type supplies its own contract (Period now, then People and Rates) | People and Rate imports without a second system |
| F2 | Staging separate from payroll | Future integrations enter the same staging engine as files |
| F3 | Value-level provenance (batch, file, sheet, row, column) until finalization, then the durable import and audit record (D7) | Tracing, explanation, audit |
| F4 | Every batch stores the column and driver mapping it used | L1 and L3 become reuse of stored data |
| F5 | Before and after values stored for every change | A later safe Undo that never reverts over newer manual edits |
| F6 | File fingerprint and canonical content fingerprint | S11 duplicate protection and L5 drift detection |
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
2. Import version 1 (§3–§8).
3. The broader Flussra visual redesign, after the pilot shows what matters.

## 11. Decisions (D)

Decided 2026-10-24.

| ID | Decision |
|---|---|
| D1 | There is no automatic default for a conflict: the user decides each one, and may apply a decision to all similar conflicts. |
| D2 | An imported value never replaces an existing value silently, whatever put that value there (a manual entry or an earlier import): the user chooses whether to overwrite it. |
| D3 | An **error** is incoming data whose destination — driver, target, date, pay item or any other required destination — Flussra cannot determine with confidence. Errors block the import. A **warning** is data Flussra understands and can place, but that looks unusual. Where understood data cannot be accepted without a choice, Review asks for it; the user either decides or deliberately excludes the affected data and continues with the rest, and an unresolved choice blocks completion. |
| D4 | Version 1 reads one row per driver per day. Other layouts (one column per day) come later as additional reader modes and must not be precluded by the design. |
| D5 | Imported work on a day whose Status is a full day off is resolved inside Review, never by sending the user out of Import. Review lists the affected drivers once each, nothing selected by default, with select all. Selecting a driver removes the conflicting full-day-off Status where imported work exists and accepts that work; otherwise that work is not accepted and is deliberately excluded. |
| D6 | Imported work on a normal day off asks whether to treat it as an exception for this period. Yes accepts it and Review notes that day-off exceptions were accepted; No excludes it and the rest continues; unanswered blocks completion. |
| D7 | The original file and detailed provenance (sheet, row, column) serve Import and Review until the period is finalized; they are not permanent payroll data. After finalization Flussra keeps the accepted payroll data and a durable import and audit record, not the file or row/column detail. |

## 12. Existing import tables

The dormant `import.*` tables predate the current architecture. They are evaluated against this plan once the implementation design exists: anything that fits is reused, anything that would bend this design is retired. Their existence is not a reason to use them.
