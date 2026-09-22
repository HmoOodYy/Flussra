# C1 Known Product and Workflow Gaps

**Project:** Flussra Payroll App  
**Scope:** Current Payroll / People / Driver Pay Rates  
**Purpose:** Record known product and workflow gaps that should be resolved in a later implementation pass.

This file intentionally describes the problems and the required product outcome only. It does **not** prescribe an implementation approach, architecture, database design, API shape, or UI design.

---

## 1. Cancelled Payroll Periods Become a Dead End

### Problem

A payroll period can currently be cancelled, after which it disappears from the normal Current Payroll workflow and becomes effectively unusable.

From the user's point of view, cancelling an unfinished payroll period should not leave behind a hidden period that can no longer be used but may still affect future payroll-period creation or date selection.

This becomes especially confusing when a user cancels a period because it was created incorrectly and then wants to create the same payroll period again from scratch.

### Required follow-up

Define and implement clear cancellation semantics for unfinished payroll periods.

The final behavior must make it unambiguous what is removed, what is retained for audit purposes, and whether the same payroll date range can be created again after cancellation.

---

## 2. Driver Pay Rate Changes After a Payroll Period Has Already Been Created

### Problem

A payroll period may be created before the user realizes that one or more driver pay rates need to change for that same period.

Example:

- A payroll period is created today.
- Later the same day, the user learns that a driver's new rate should apply from the beginning of that payroll period.
- The user may then feel forced to choose between recreating the payroll period or entering an earlier effective date simply to make the new rate affect the existing payroll.

The current product does not make this workflow clear enough.

There is also a broader usability issue around the difference between:

- when a rate record is created;
- when that rate is effective for payroll;
- which already-created payroll periods may be affected by that rate.

This can become especially confusing when multiple drivers require corrections or rate changes during the same payroll cycle.

### Required follow-up

Define a clear and safe workflow for rate changes or corrections that are discovered after a payroll period already exists.

The final behavior should make it obvious to the user how a new or corrected rate affects an existing unfinished payroll period without requiring misleading dates or unnecessary payroll-period recreation.

---

## 3. Payroll Period Creation vs. Branch Payroll Setup

### Problem

The intended workflow requires a branch to have valid Payroll Setup before a payroll period can be created.

However, a previous smoke-test scenario appeared to create a payroll period for a branch that did not visibly have Payroll Setup configured.

The current backend contains setup prerequisites, but the earlier observed behavior creates uncertainty about whether all product paths, test fixtures, legacy paths, and UI flows enforce the same prerequisite consistently.

This makes the ordering of the workflow unclear:

**Branch → Payroll Setup → Payroll Period**

### Required follow-up

Verify the complete product workflow from a clean state and resolve any path that allows payroll-period creation before the required branch Payroll Setup exists.

The expected prerequisite ordering should be consistent across the backend, frontend, tests, fixtures, and smoke-test flows.

---

## 4. People, Drivers, and Driver Pay Rates Do Not Present One Clear Workflow

### Problem

The current application exposes related concepts through different data paths:

- the current People screen is centered on application users/accounts;
- Driver Pay Rates operates on operational driver records;
- the backend also has employee/driver records independent of whether a user account exists.

As a result, a driver may legitimately appear in Driver Pay Rates while not appearing in the current People screen in the way a user would expect.

This creates a confusing product workflow because the user cannot clearly tell which step is supposed to happen first:

**Person → Driver → Pay Rates**

or whether creating a driver separately is expected.

A previous smoke test exposed this confusion by showing a driver in Driver Pay Rates without an obvious corresponding People entry.

### Required follow-up

Define one clear product workflow and source of truth for People, Employees, Drivers, and Driver Pay Rates.

The final application should make the dependency/order between these concepts obvious to the user and prevent records from appearing to exist "out of sequence" across different screens.

---

## 5. C1 Status

These items are known gaps to revisit after the current C1 foundation work.

They should be reviewed and classified before the project moves too far into pilot usage so that true workflow blockers can be separated from UX improvements and non-blocking cleanup.

No implementation decision is made by this document.
