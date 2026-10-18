## Plan: Durable Driver identity, Driver-owned compensation, payable rules

**Order: Stage 1 → Stage 2 → Stage 3 → P4c.** All three stages must land before P4c, because P4c freezes driver identity, rate identity and payable amounts into evidence and the hash.

### The model (target)

```
Employee            the person in the company (unchanged; not every employee drives)
 └─ Driver          permanent driver identity, 0..1 per Employee, same company
     │              owns: driver number, CDL, rates, Min/Max
     └─ DriverAssignment[]   one continuous home/primary-Branch tenure, with start/end instants
                    defines: the Driver’s home Branch and permanent-transfer provenance
```

- A permanent transfer **closes** one home `DriverAssignment` and **opens** a new one at the transfer `EffectiveAt`. The Driver never changes.
- A temporary loan **does not close, replace or create a home `DriverAssignment`**. The Driver’s home Branch remains unchanged. A future loan feature may record separate temporary host-Branch work/provenance, but that is not built in these stages.

### Shared ground rules (all stages)

- **Pre-production clean cutover is allowed**, as in P4b. Dev data may be reset. A migration must refuse to run, with no changes made, rather than silently reinterpret existing payroll data.
- **No dual authority.** At the end of each stage, every concept has exactly one owner.
- **Payroll stays per branch.** Periods, daily work/pay attribution, approval and evidence remain Branch-owned. `DriverAssignment` is the permanent home-Branch staffing context; do not encode a permanent invariant that all future work must occur in that same Branch, because a future temporary-loan path may carry separate host-Branch provenance without changing the home assignment.
- **No new calculation methods, no formula engine, no generic frameworks** (no alias system, no proration framework, no secondary `DriverAssignment` framework, no loan framework in these stages).
- **Locked contracts are amended before code.** This is commit 1 of the Stage 1 PR, not a separate PR.
- Each stage ends with the full `validate.ps1` gate green, plus its own tests.

---

### Stage 1: Durable Driver identity + Driver Assignments

**The leader leads to:**

- **Commit 1, docs only:** amend the unified plan and the People/Workforce/Access contract with **every** decision from this round:
  - the Driver / home-DriverAssignment model
  - permanent transfer vs temporary loan semantics
  - compensation owned by the Driver
  - authority by effective date / effective instant as appropriate to the canonical temporal contract
  - Min/Max bound to the Driver’s home pay calendar; a temporary loan never changes that calendar
  - a permanent home-Branch change to an incompatible pay calendar makes §8.8’s advisory notice a **blocker**
  - the rounding and proration rules
  - the G0.1 change for typed-in amounts
  - P6a (transfer compensation copy) retired
  - the new P5 criterion that a transfer never requires re-entering status rates
  - a temporary loan is not a home `DriverAssignment` and does not change the Driver’s home Branch
- **The permanent `Driver` exists**: one per driving Employee, same company. It owns the **driver number** (stable, unique per company) and the **CDL number** (a scalar current value is fine for v1).
- **Today’s branch profiles become home `DriverAssignment`s** with a `DriverAssignmentID`, and the misleading “DriverID = profile” meaning is gone from the schema, services, APIs and frontend. Their effective boundaries follow the merged temporal contract: instants, not DATE-only identity boundaries.
- **A permanent transfer creates a new home assignment under the same Driver at `EffectiveAt`.** No generated `DRV-TR…` codes, nothing person-level lost.
- **A temporary loan leaves the home assignment unchanged.** Do not model it as a permanent transfer just to make the Driver visible in another Branch.
- **The effective-assignment resolver** (today `fn_EffectiveDriverProfile`) answers which home assignment is effective for this Driver at an instant. Business-date payroll consumers continue to use the canonical Branch-local WorkDate/overlap rules rather than inventing a Company or browser clock.
- **Self/DRIVER authorization** resolves User → Employee → Driver → effective assignment.
- **User-facing driver identity comes from the Driver.** Payroll screens still show the assignment and branch context where it explains where the money came from.

**The implementer must think about:**

- **Every consumer of today’s DriverID** must be classified as either “means the person-driver” or “means the home Branch assignment”. The list includes:
  - draft lines, eligibility rows and their transfer-link snapshot columns
  - bonus events, pay rules and rate assignments
  - calculation snapshots, final lines and evidence
  - reports, the hub, review and the finalized library
  - transfer, workforce, core reads and dashboard counts
  - the P2b route authorization inventory and the frontend types
  Most of these are **home assignment** today. Nothing may be renamed blindly, and Branch payroll/work provenance must not be semantically collapsed into the home assignment in a way that would block a future loan path.
- **The DB protections stay, on home assignments:**
  - no overlapping assignments per Driver
  - an assignment’s branch can’t change
  - closed history can’t be reopened
  - future-effective (pending) assignments
  - the never-effective terminated case
- **Status ownership:** today `DriverStatus` mixes assignment-closing reasons (Transferred, Terminated) with work state (Active, OnLeave). Decide which states belong to the assignment and which to the Driver or Employee. Don’t guess from names.
- **Driver ↔ Employee link:** same company and immutable. Decide when a Driver is created (with its first assignment) and how “stops driving, comes back later” (DWR-022) reuses the same Driver. Don’t build rehire now; just don’t block it.
- **Evidence and the hash:** the shape that P4c will freeze must be able to carry `DriverAssignmentID`, `DriverID`, `EmployeeID`, `BranchID` and the **driver number snapshot**. Today `DriverCodeSnapshot` is part of `snapshot_hash.py`; keep the hash reproducible. `BranchID` is payroll/work provenance and must not be defined in a way that makes a future host-Branch loan impossible; a future loan design may need to freeze home-Branch and host/work-Branch provenance separately.
- **ExternalDriverID and `core.DriverAliases`:** don’t build a generic alias or external-ID system. Move ExternalDriverID only if its meaning is clearly defined; otherwise defer both to C2 import, as source system + external key + Driver.
- **Naming collision:** `DriverRateAssignments` (rates) vs `DriverAssignments` (branch) will confuse people. Flag it; Stage 2 decides the rate table’s final name.
- **The loan rule is documentation only in this stage.** The concrete loan/worked-for-Branch record, eligibility path and UI are deferred. The architecture must only preserve the rule that temporary host work does not replace the home `DriverAssignment`.

**Out of scope:** moving compensation ownership (Stage 2), payable rules (Stage 3), a UI redesign beyond identity and labels.

**Done when:**

- A permanent transfer keeps the same Driver, driver number and CDL, closes one home assignment and adds another at `EffectiveAt`.
- No code generates per-assignment driver codes.
- Every former DriverID usage is explicitly an assignment or a Driver.
- Self authorization works through the Driver.
- The fresh-upgrade and schema-authority checks pass.

#### Stage 1 implementation decisions

Recorded by the Lead on 2026-10-18 before implementation. They resolve the "implementer must think about" items above and do not alter any approved decision.

**Identity model**

- `core.Employees` is unchanged. The new `core.Drivers` is the permanent Driver: `DriverID`, `CompanyID`, `EmployeeID` (UNIQUE, so 0..1 Driver per Employee; composite FK `(EmployeeID, CompanyID)`), nullable `DriverNumber` (unique per company when present; never generated per assignment), scalar `CDLNumber`, `DriverStatus`, timestamps. `CompanyID` and `EmployeeID` are immutable.
- A Driver is created in the same Workforce operation as its first `DriverAssignment`. A driver who stops driving and comes back later (DWR-022) reuses the same Driver with a new assignment; the rehire flow itself is still not built.
- Today's `core.Drivers` rows become `core.DriverAssignments`: one continuous home-Branch tenure with `CompanyID`, immutable `BranchID`, immutable `DriverID`, `EmployeeID` (an FK-enforced copy via `(DriverID, EmployeeID, CompanyID)`, so it can never disagree), `EffectiveFromAt` / `EffectiveToAt` (half-open `TIMESTAMPTZ` instants, as in migration `0086`), `EndReason`, and transfer lineage (`TransferredFromAssignmentID` / `TransferredToAssignmentID`, same Driver, same company).
- `DriverCode`, `CDLNumber` and `DriverStatus` leave the assignment (to `DriverNumber`, `CDLNumber` and `DriverStatus` on the Driver, and `EndReason`, respectively).
- No overlapping assignment windows per Driver (instant-range exclusion). The history protections stay: an ended assignment is immutable; a future boundary may change only for transfer cancel, transfer reschedule or termination; a never-effective destination assignment may be deleted only by transfer cancel.
- The resolver is `core.fn_EffectiveDriverAssignment(company, driver, at)`. Employee-addressed callers map Employee to Driver (0..1) first.
- `ExternalDriverID` is dropped (no defined meaning, no consumer) and `core.DriverAliases` is dropped (no application consumer). External identities and aliases are designed in C2 Import as source system + external key + Driver. `import.ImportRows.MatchedDriverID` is repointed to the permanent Driver.

**DriverID classification**

After Stage 1, `DriverID` means only the permanent Driver. Everything that stored a branch-profile id becomes `DriverAssignmentID`: payroll drafts, day-entry state, eligibility, bonus events, final lines, calculation totals, snapshots, audit evidence, and (for Stage 1 only, unchanged in ownership) the legacy and target rate/pay-rule tables, which Stage 2 re-keys to the Driver. Evidence and final tables also carry the permanent `DriverID` (bound to the assignment by composite FK) and `DriverNumberSnapshot` in place of `DriverCodeSnapshot`. The rate-table naming collision flagged above stays with Stage 2.

**Status ownership**

- `Driver.DriverStatus` is `Active`, `Inactive` or `OnLeave`: the durable work state of the person-driver. It survives transfers.
- `DriverAssignment.EndReason` is `NULL`, `Transferred` or `Terminated`, written together with `EffectiveToAt`. `Employee.EmploymentStatus` is unchanged.
- Termination does not change `Driver.DriverStatus`; `Employee.EmploymentStatus` and the `Terminated` assignment window govern.
- An assignment is lifecycle-eligible for payroll when it has an `EndReason` (historical) or its Driver is `Active`. Eligibility classification derives from `EndReason`, and the Driver status is snapshotted.

**Transfer lifecycle**

- A permanent transfer ends the source assignment and opens the destination assignment of the same Driver at one instant. Immediate transfers use database `NOW()` at completion and are `Completed`.
- A future-dated transfer materializes both windows when it is completed (the destination stays pending) and is `Scheduled`, not `Completed`. The windows are the single authority, so currentness switches at `EffectiveAt` by time alone, with no job. One SQL function, `core.fn_DriverTransferStatus`, reports a stored `Scheduled` request as `Completed` once `EffectiveAt` has passed; every read, filter and active-transfer check uses it.
- A `Scheduled` transfer can be cancelled or rescheduled only while `EffectiveAt` is in the future at one wall-clock decision instant (`clock_timestamp()`) read after all locks, and only while the Employee is not terminated. Cancel deletes the never-effective destination assignment and reopens the source; reschedule moves both boundaries and keeps the destination assignment ID. Both are refused if payroll facts or review/approved/locked/archived periods would be affected; `Cancelled` periods never block.
- Termination is authoritative: a transfer whose `EffectiveAt` is at or after the source's termination cutoff is `Cancelled` through the cancel disposal routine (no destination remains, the source ends `Terminated` at its cutoff); one before the cutoff stays, becomes effectively `Completed` at `EffectiveAt`, and can no longer be cancelled or rescheduled.
- Coverage-shrinking operations (complete, cancel, reschedule, termination) hold the overlapping PayrollPeriod row locks before changing windows; source writers re-check eligibility under the period lock.

**Eligibility re-capture rule**

Cancel and reschedule may re-capture the named assignment's eligibility row, with the same snapshot SQL as the append rule, only in `Draft`, `Open` or `Returned` periods of that assignment's Branch overlapping the affected interval. If a period in review, approved, locked or archived state overlaps, the operation is refused. A `Cancelled` period never blocks and is never re-captured (it cannot carry submissions or evidence; a disposed destination's roster rows in it are deleted). This is the single narrow exception to the rule that existing eligibility rows and frozen date arrays are left untouched.

---

### Stage 2: Driver-owned compensation

**The leader leads to:**

- **Ordinary driver rate schedules and values are owned by the permanent `Driver`.** One effective schedule per Driver per rate definition.
- **DriverPayRules (Min/Max) are owned by the Driver**, and each rule records the **home pay calendar** it was set for: frequency, interval and anchor. While the same home `DriverAssignment` remains effective, temporary work for another Branch does not change that Min/Max calendar.
- **The resolver answers by Driver:** (DriverID, rate definition, work date). Payroll gets the Driver from the assignment.
- **Ownership boundaries stay:**
  - PayDefinitions stay **company-owned**.
  - Branch applicability stays **branch-owned**. Whether a rate applies is decided by the period’s frozen definitions.
  - Status rate columns stay **branch-owned**; P5 decides how a driver’s status values bind to them.
- **Authority:** authoring and approval belong to the home Branch of the **assignment effective for the rate’s start-date context**, plus company-wide roles. A future permanent transfer that isn’t yet authoritative gives the target Branch no authority. History is read-only.
- **Permanent transfers carry Driver-owned compensation over automatically.** The P6 copy design and the legacy copy route are retired.
- **Temporary loans do not transfer compensation ownership and do not change the Min/Max home pay calendar.**

**The implementer must think about:**

- **One canonical finalized-period guard.** “Does this window touch a finalized period in **any** branch, through **any** assignment of this Driver?” Rates, pay rules and the payroll guards use the same helper. Today there are separate branch-only versions in `assignments.py`, `driver_pay_rules.py` and payroll/guards.py.
- **Authoring applicability** (`_require_authoring_authority`) checks the branch on the start date today. Decide what “authorable” means for a Driver-owned rate; the per-branch decision moves to calculation, where missing ≠ zero still blocks.
- **The overlap, single-pending and single-approved rules** move to the Driver **without weakening concurrency**. Respect the lock order documented in `branch_compensation_config_concurrency.py`.
- **The currency gate and monetary locks** (P3a/G0.3) apply to every Driver-owned write.
- **The home pay calendar on Min/Max is a compatibility key, not a rescaling basis.** No frequency-conversion maths, ever. A temporary loan to a host Branch with a different calendar is **not** a Min/Max calendar mismatch because the home assignment did not change. A permanent home-Branch transfer to an incompatible calendar is a blocker until the rule is explicitly reconciled.
- **Visibility:** what an old branch’s manager can see of a transferred driver’s rate history. Decide it, and add negative tests.
- **Frontend Pay Rates** lists drivers per branch today (`usePayRateDriverContext`, `PayRateDriverList`). It must list **Drivers**, with the authority rule above. Nothing more until P7.
- **Rate table naming:** settle the collision flagged in Stage 1.
- **Tests that assume per-profile rates** (the `test_p3c_*`, `test_p4a_*` and `test_p4b_*` resolver and calculation suites) are reworked deliberately, never deleted to get green.

**Out of scope:** building status pay (P5), OrdinalTier (P5b), the loan tag, rounding and proration (Stage 3).

**Done when:**

- A permanently transferred Driver’s rates and Min/Max continue with zero manual steps when the new home pay calendar matches.
- A temporary loan leaves the Driver’s home Min/Max calendar unchanged.
- Nothing compensation-related is keyed by assignment.
- One finalized guard protects every branch.
- The copy route is gone.

---

### Stage 3: Payable rules in the live calculation

**The leader leads to:**

- **Rounding**, applied in the live packet and preview:
  - ordinary earnings per DriverAssignment × PayDefinition × period, summed first
  - bonus per DriverAssignment × period
  - the Min/Max adjustment
  Each is rounded once with `ROUND_HALF_EVEN` to the company currency’s stored ISO decimals. Driver total = sum of the official components, never re-rounded.
- **Min/Max proration:** fraction = eligible scheduled days ÷ scheduled days of the Driver’s **home-Branch period** for the guarantee being evaluated.
  - Unscheduled days never count, even if worked.
  - A day whose status key has `IsOffReason` doesn’t count.
  - Whole days only.
  - The same fraction for Min and Max.
  - The prorated amount stays at internal precision until the one official rounding.
- **Pay-calendar compatibility:** a Min/Max rule applies against the **frozen home-Branch pay calendar** (`FrozenPayrollFrequency`, `FrozenAnchorStartDate`, `FrozenCustomIntervalDays`) for the guarantee window. A mismatch caused by a permanent home-Branch/pay-calendar change is a **payroll blocker**: never rescaled, never silently dropped, never duplicated. A temporary loan’s host-Branch calendar does not participate in this compatibility check.
- **Typed-in Bonus, Minimum and Maximum amounts** are rounded to currency precision on entry, and the result is shown to the user. This is the recorded G0.1 change.
- **The packet exposes**, for every official amount, its pre-rounding value, its official value and the rule versions, plus the proration’s top, bottom, version and the pay calendar used, so P4c freezes it **without recalculating**.

**The implementer must think about:**

- **Internal precision stays at `0.0001`.** Rates and quantities are never rounded.
- **One rounding per official amount.** Watch the hub, reports, read models and `money.ts`; none of them may become a rounding authority.
- **“Scheduled day” comes from the frozen `PayrollPeriodDays` of the home-Branch guarantee period**, not live Payroll Setup and not a temporary host Branch’s calendar.
- **Off days come from the explicit `IsOffReason` flag** (already snapshotted), never from status names.
- **Min/Max rule selection** keeps v1 semantics (the rule effective at period start), now with the calendar check.
- **The current Min/Max loop runs one query per driver** (`period_calculation.py` \~529–557). It may become set-based only if behaviour stays identical.
- **Edge cases with tests:**
  - a driver starting on a weekend (fraction 0)
  - a period with zero scheduled days
  - a **permanent transfer** between two home Branches on the **same** calendar (sums to about one minimum, subject to rounding)
  - a **permanent transfer** between **different** home calendars (blocker)
  - a permanent home-Branch transfer with the same frequency but a different anchor (blocker)
  - a home Payroll Setup version changing frequency (blocker)
  - future temporary-loan support must not switch Min/Max to the host Branch calendar merely because work is attributed there
  - Min > Max after proration (still a hard blocker)
  - JPY (0 decimals) and KWD (3 decimals)
  - a negative Max adjustment
- **Existing 4-decimal Bonus and pay-rule values in dev data:** reset or refuse, never silently rewrite.
- **The frontend shows official amounts with exactly the currency’s decimals**, and rates with their own precision.
- **Nothing here unlocks submit or finalize.** The evidence gate stays closed until P4c.

**Out of scope:** freezing into evidence and the hash, finalization, reports and ledger (P4c); export; the concrete temporary-loan/worked-for-Branch feature; half-day leave and hours-per-day schedules (deferred).

**Done when:**

- Preview and live totals follow both rules exactly.
- Every official amount has valid currency precision.
- Permanent home-calendar mismatches block instead of guessing; temporary host work never changes the home Min/Max calendar by itself.
- The packet carries everything P4c needs.

---

### After the three stages

**P4c** freezes Stage 3’s packet into immutable evidence and the hash, including `DriverAssignmentID`, `DriverID`, `EmployeeID`, `BranchID` and the driver number snapshot. It rebuilds the deleted submit/finalize/report suites and opens the evidence gate, with **no open product decisions left inside the current P4c scope**. The concrete temporary-loan/worked-for-Branch feature remains explicitly deferred, but P4c must not encode identity or Branch-provenance semantics that make that future path impossible.

**P5** must meet the recorded criterion: *a transfer never requires re-entering status rates.*
