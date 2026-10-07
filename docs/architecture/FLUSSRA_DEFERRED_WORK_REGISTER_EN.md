# Flussra — Deferred Work Register

> \*\*Purpose of this file:\*\* This is the official register for work that was intentionally deferred during the refoundation, so we do not forget:
> what the issue is, where it was discovered, why it was deferred, which phase owns it, and whether it has actually been resolved.

\---

# 0\. Rules for Using This File — IMPORTANT FOR ANY AI WORKING ON THE PROJECT

## Update Rule

Any AI working on Flussra that resolves an issue listed in this register **must update the existing entry itself** instead of leaving it unchanged or creating a duplicate note elsewhere.

When an issue is actually resolved, change:

```text
Issue Resolved?: YES
```

and also fill in:

```text
Resolved In: <Work Unit / Phase>
Resolution Evidence: <Commit / PR / Tests / Migration / File>
Last Updated: <YYYY-MM-DD>
```

## When Is It Valid to Write YES?

Do **not** write `YES` merely because local code has been written.

Write `YES` only when:

1. the required solution has been fully implemented;
2. the implementation has been reviewed and accepted by the Lead;
3. the required validation/tests for that phase have passed;
4. if the work requires a merge, it has been merged or the Lead has explicitly closed the work item.

If the solution is only partial, or a temporary compatibility seam still exists:

```text
Issue Resolved?: NOT YET
```

and update `Current State` to explain what has already been done and what is still outstanding.

## Never Delete Old Entries

When an issue is resolved, **do not delete the entry**.

The purpose of this file is to preserve history so we can later determine:

* what the issue originally was;
* why it was deferred;
* when it was resolved;
* how it was resolved.

## Adding a New Deferred Issue

If a new issue is discovered during any phase and the Lead intentionally defers it, append a new entry to the Deferred Work Entries section using this structure:

```markdown
## DWR-XXX — <Short Name>

\*\*Issue:\*\*  
<Description of the issue>

\*\*Discovered In:\*\*  
<Phase / Work Unit / subsystem>

\*\*Deferred To:\*\*  
<Phase / Work Unit>

\*\*Why Deferred:\*\*  
<Reason for deferral>

\*\*Current State:\*\*  
<What exists today and what remains>

\*\*Issue Resolved?:\*\* NOT YET

\*\*Resolved In:\*\* —
\*\*Resolution Evidence:\*\* —
\*\*Last Updated:\*\* YYYY-MM-DD
```

## If the Target Phase Changes

If an issue is officially moved to a different work unit:

* do not erase the historical context;
* update `Deferred To` to the new target;
* explain in `Current State` that the target changed and why.

\---

# 1\. Current Reference State

**Architecture authority:**

* `FLUSSRA\_UNIFIED\_REFOUNDATION\_EXECUTION\_PLAN.md`
* `PEOPLE\_WORKFORCE\_ACCESS\_ARCHITECTURE\_CONTRACT.md`
* `PEOPLE\_AND\_ACCESS\_REFOUNDATION\_MASTER\_PLAN.md`

**P1a:** CLOSED / merged.

**P1b:** CLOSED / merged.

**P1c:** CLOSED / merged.

**P2a:** CLOSED / merged (PR #24; migration `0074`).

**P2b:** CLOSED / merged (PR #26; migration `0075`).

**Pre-P3 Compensation Architecture Amendment:** merged.

**P2c:** CLOSED / merged (PR #28; merge `af00a22915f93810d123aadea18c3a73da1360aa`).

**P3a:** CLOSED / merged (PR #30; migration `0076`).

**G0:** CLOSED (G0.1 PR #34; G0.2 PRs #35–#39; G0.3 PR #40; G0.4 PRs #41–#43; G0.5 PR #44; G0.6 PR #45).

**P3b:** CLOSED / merged (PR #46; migration `0082`; Target Compensation schema/invariants).

**P3c:** CLOSED / merged (PR #47; migration `0083`; dormant target Compensation authoring and resolver).

**P4a:** CLOSED / merged (PR #48; `main` at `83234d46b5ab357c1e7062d77aff4e4ad3d9e711`; generic PayDefinition / Branch Configuration authority cutover; migration `0084`).

**P4b:** Current implementation work unit (Target Period Definition Runtime + PerUnit Cutover; migration `0085`).

**Post-G0 accepted baseline:** `main` at the PR #45 merge, `f2937bfdcbe1266a00716905a8712f3fe29baff2`. P3b merge baseline: `main` at the PR #46 merge, `edcf871f59d0616e194a8e752d9b6ba23f42b8a2`. P3c merge baseline: `main` at the PR #47 merge, `3f389499bbb1a0a70935360f32230f8e0aae4a7c`.

**Test Hygiene:** PR #29 merged after P2c.

**Migration heads:** pre-P3b `0081`; P3b `0082`; P3c `0083`; P4a advanced it to `0084`; P4b advances it to `0085`.

**Important rule:** Anything below marked `NOT YET` remains intentional architectural/product debt until the entry itself is updated to `YES`.

\---

# 2\. Deferred Work Entries

## DWR-001 — Workforce Write Authority Did Not Exist as an Independent Domain

**Issue:**  
Before P1b, Employee / Driver creation and mutation responsibilities were distributed across Core and Admin instead of being owned by one canonical Workforce authority.

**Discovered In:**  
P1a / Workforce authority review after database invariants were established.

**Deferred To:**  
**P1b — Workforce write authority.**

**Why Deferred:**  
P1a was intentionally limited to database invariants and the effective-profile resolver. Combining the authority cutover into P1a would have mixed two large dependency-closed work units and made review and regression isolation much harder.

**Current State:**  
Resolved.

`backend/app/workforce/\*` is now the canonical owner of non-transfer Employee and Driver-profile mutations.

Core and Admin compatibility paths no longer act as independent Workforce write authorities and instead delegate/orchestrate through the Workforce domain where temporary compatibility is still required.

**P1b also established:**

\- explicit Workforce Employee / Driver-profile APIs;

\- Employee support independent of Users;

\- stable Workforce-owned EmployeeKey generation;

\- effective-dated current/pending Driver-profile handling;

\- protection against generic mutation of historical Driver profiles;

\- Workforce audit evidence;

\- `employees.view` / `employees.manage` runtime authority;

\- removal of `EmployeeType` and `drivers.manage` as runtime authorities.

**Final validation passed with:**

\- 3257 backend tests passed

\- 4 skipped

\- 0 failed

\- 0 errors

\- Ruff passed

\- `git diff --check` passed

\- frontend lint passed

\- frontend build passed

\- migration `0073` fresh upgrade passed

\- `0073 -> 0072` downgrade passed

\- `0072 -> 0073` re-upgrade passed

**P1b was committed as:**

`eaab88e — feat: establish workforce write authority`

**and merged through:**

`PR #20 — feat: establish workforce write authority`

**Merge commit:**

**`3996089b83cc119839237b07f2bad4f51a724ca6`**



**Issue Resolved?:** YES

**Resolved In:** P1b — Workforce write authority    
**Resolution Evidence:** PR #20, merge commit `3996089b83cc119839237b07f2bad4f51a724ca6`, and final validation (`3257 passed, 4 skipped, 0 failed`)  
**Last Updated:** 2026-09-29

\---

## DWR-002 — Some Current-Driver Reads Still Depend on Status Instead of Effective-Date Authority

**Issue:**  
Some runtime queries historically determined the current Driver from `DriverStatus` or patterns similar to:

```sql
DriverStatus NOT IN ('Transferred', 'Terminated')
```

instead of using the effective-date resolver.

**Discovered In:**  
P1a while introducing `fn\_EffectiveDriverProfile` and the Workforce resolver.

**Deferred To:**  
**P1c and the later effective-profile / security cutovers** according to the unified execution plan.

**Why Deferred:**  
Converting every consumer in one work unit would have expanded P1a/P1b into Core, Admin, Dashboard, Transfer, Access, and security behavior at once.

**Current State:**  
P1c moved transfer, termination, Workforce projections, and payroll eligibility onto effective-date lifecycle semantics. P2b also centralized current DRIVER/Self ownership resolution. The issue is not fully closed: status-based reads remain in compatibility and operational views, including dashboard/current-hub and settings summaries, and legacy period-eligibility readers remain subject to the Unified Plan's P8a consumer audit. The Unified Plan identifies these as remaining readers rather than current-profile resolvers; any DRIVER-facing access boundary was addressed in P2b.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-10-02

\---

## DWR-003 — `POST /core/drivers` Still Exists as a Legacy Compatibility Route

**Issue:**  
The target architecture does not need Core to remain a Workforce creation owner, but the old route is still consumed by callers/tests.

**Discovered In:**  
P1b — Core → Workforce authority cutover.

**Deferred To:**  
**P8a — Runtime legacy deletion.**

**Why Deferred:**  
Removing the route before all consumers are migrated would create breakage without architectural value. The immediate goal is to remove the old authority, not to break compatibility early.

**Current State:**  
The route must be a thin adapter to the Workforce service with no independent Employee/Driver creation logic.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-004 — `PATCH /core/drivers/{id}` Still Exists as a Compatibility Endpoint

**Issue:**  
The old Core Driver PATCH endpoint remains, while the target mutation owner is Workforce.

**Discovered In:**  
P1b.

**Deferred To:**  
**P8a — Runtime legacy deletion.**

**Why Deferred:**  
Existing callers still depend on the route. Removing it now would mix API-consumer migration with authority cutover.

**Current State:**  
After P1b, the route should accept Driver-profile-owned fields only and delegate to the Workforce owner. Employee-owned fields must no longer be handled through this endpoint.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-005 — `/core/people` Still Exists as a Compatibility Read Surface

**Issue:**  
`/core/people` is a legacy API surface, while the target Employee read authority is Workforce.

**Discovered In:**  
P1b.

**Deferred To:**  
**P7a** for UI consumer migration, then **P8a** for final deletion if zero consumers are proven.

**Why Deferred:**  
The frontend and some runtime consumers still use it.

**Current State:**  
The route must act as a compatibility read over the Workforce read model and use `driver\_state=current|pending|none` instead of `employee\_type`.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-006 — `ensure\_driver\_profile` Still Exists as a Temporary Access Adapter

**Issue:**  
Historically, role assignment could create an Employee, create a Driver profile, and link a User as part of the Access flow. That violates the final Access/Workforce boundary.

**Discovered In:**  
P1a/P1b while separating Workforce authority.

**Deferred To:**  
**P2c — Remove Access → Workforce effects.**

**Why Deferred:**  
Deleting the behavior completely in P1b would prematurely implement the Access refoundation before explicit linking/provisioning exists.

**Current State:**  
Resolved. The P2c implementation removed `ensure_driver_profile` and the `assign_company_role` hook that called it. DRIVER/Self assignment requires the existing explicit same-company Employee link and a current effective Driver profile; Access assignment does not create Workforce state or implicitly link the User.

**Issue Resolved?:** YES

**Resolved In:** P2c — Remove Access → Workforce effects  
**Resolution Evidence:** `backend/app/admin/service.py`; `backend/tests/test_p2c_access_workforce_boundary.py`; focused P2c validation (55 passed); canonical full backend run (3,315 passed, 5 failed, 3 skipped, 0 errors; no P2c regression identified); final Lead approval on 2026-10-02. The five unrelated full-suite failures were not fixed by P2c.  
**Last Updated:** 2026-10-02

\---

## DWR-007 — User ↔ Employee Linking Is Still Implicit / Not Yet Complete as an Access Contract

**Issue:**  
The target architecture needs an explicit optional one-to-one relationship between User and Employee, with same-company enforcement and defined link/unlink/relink rules.

**Discovered In:**  
P1b while separating Employee from User.

**Deferred To:**  
**P2a — User–Employee link + staged provisioning.**

**Why Deferred:**  
P1b owns Workforce only. Adding provisioning and link lifecycle rules would mix Access and Workforce in one work unit.

**Current State:**  
P2a established the explicit optional same-company User↔Employee link, link/unlink/relink authority, staged login-disabled accounts, and atomic staged provisioning. Explicit employee selection in the People provisioning flow remains supported. Workforce endpoints do not create Users; Access role assignment does not implicitly create or link Workforce records.

**Issue Resolved?:** YES

**Resolved In:** P2a — User–Employee link + staged provisioning  
**Resolution Evidence:** PR #24, merged commit `d7dc63a4e1ceb461e4e1fcbb2abe3c4fc449abea`, migration `0074`, and `backend/tests/test_p2a_access_provisioning.py`  
**Last Updated:** 2026-10-02

\---

## DWR-008 — DRIVER Self-Scope Security Contract Was Not Yet Complete

**Issue:**  
The target security model requires:

```text
DRIVER ⇔ Self
```

with own identity derived through User → Employee → effective Driver profile, and fail-closed behavior when no current profile exists.

**Discovered In:**  
People/Access architecture work before and during P1b.

**Deferred To:**  
**P2b — DRIVER/Self authorization hardening.**

**Why Deferred:**  
P1b establishes Workforce authority. It does not redesign Access/self-service authorization.

**Current State:**  
P2b established generic Self scope with no Access BranchID and the current-product DRIVER ⇔ Self binding. Self ownership resolves through User→Employee→effective Driver profile; generic DRIVER/Self permissions remain subject to the capability ceiling and fail-closed behavior. OwnDriverDataOnly is transitional legacy state, not the target scope.

**Issue Resolved?:** YES

**Resolved In:** P2b — DRIVER/Self authorization hardening  
**Resolution Evidence:** PR #26, merged commit `819fb9ae4a59b2c4835778c45e4f8bc49fdc0f45`, migration `0075`, and the Self authorization/security and route-inventory tests  
**Last Updated:** 2026-10-02

\---

## DWR-009 — Access Still Has Side Effects That Touch Workforce

**Issue:**  
Role/Scope operations should eventually be Access-only, but transitional behavior can still invoke Workforce mutation and User linking.

**Discovered In:**  
P1b.

**Deferred To:**  
**P2c — Remove Access → Workforce effects.**

**Why Deferred:**  
The Workforce owner must be established first, then explicit Access linking/provisioning must exist, and only then can this coupling be removed safely.

**Current State:**  
Resolved. Access role/scope writers are Access-only: assignment, replacement/swap, revocation, legacy role writes, and owner transfer do not create or mutate Employee/Driver records or implicitly link/unlink `User.EmployeeID`. Explicit employee-link operations remain intentionally valid.

**Issue Resolved?:** YES

**Resolved In:** P2c — Remove Access → Workforce effects  
**Resolution Evidence:** `backend/app/admin/service.py`; behavioral boundary coverage in `backend/tests/test_p2c_access_workforce_boundary.py` (55 focused P2c/P2a/P2b/owner-transfer tests passed); canonical full backend run (3,315 passed, 5 failed, 3 skipped, 0 errors; no P2c regression identified); final Lead approval on 2026-10-02. The five unrelated full-suite failures were not fixed by P2c.  
**Last Updated:** 2026-10-02

\---

## DWR-010 — Transfer Completion Authority Has Not Yet Been Fully Cut Over to the Effective-Dated Workforce Lifecycle

**Issue:**  
The Driver Transfer flow still needs final semantics for source/destination effective windows, future effective dates, projections, locking, current/pending resolution, and preventing premature Employee branch movement.

**Discovered In:**  
P1a after effective-window invariants were introduced; intentionally remained out of scope in P1b.

**Deferred To:**  
**P1c — Transfer / Workforce lifecycle cutover.**

**Why Deferred:**  
Transfer completion is a high-risk lifecycle transaction with separate concurrency, history, projection, and security concerns from Workforce CRUD.

**Current State:**  
P1c cut over transfer completion to the effective-dated Workforce lifecycle: future/current transfers preserve source and destination windows, Employee branch projection follows the effective date, and completion/termination operations preserve transaction and concurrency invariants. The legacy completion path was removed; physical cleanup remains assigned to later phases.

**Issue Resolved?:** YES

**Resolved In:** P1c — Transfer / Workforce lifecycle cutover  
**Resolution Evidence:** PR #22, merge commit `3d786306b60374a85a03e42a688eb90ef26754b0` (implementation `bda65e2`); `backend/tests/test_p1c_transfer_termination_cutover.py` and `backend/tests/test_p1c_transfer_termination_concurrency.py`  
**Last Updated:** 2026-10-02

\---

## DWR-011 — Driver Employee Termination Is Not Yet Implemented as an Atomic Workforce Operation

**Issue:**  
Correct termination requires one business transaction spanning Employee state, current/pending Driver profiles, transfer state, and payroll history protection.

The target core behavior is:

```text
Employee:
EmploymentStatus = Terminated
TerminationDate = D

Driver profile:
DriverStatus = Terminated
EffectiveTo = D
```

**Discovered In:**  
P1a/P1b lifecycle review.

**Deferred To:**  
**P1c / Workforce lifecycle cutover** according to the unified execution plan.

**Why Deferred:**  
Termination is not a generic PATCH. It affects effective windows, pending profiles, transfers, eligibility, history, and authorization.

**Current State:**  
P1c implemented Driver Employee termination as an atomic Workforce lifecycle operation, including effective-date handling, pending-transfer/profile behavior, payroll-history protection, and rollback on audit failure. Generic Employee editing remains separate from Driver termination.

**Issue Resolved?:** YES

**Resolved In:** P1c — Transfer / Workforce lifecycle cutover  
**Resolution Evidence:** PR #22, merge commit `3d786306b60374a85a03e42a688eb90ef26754b0` (implementation `bda65e2`); `backend/tests/test_p1c_transfer_termination_cutover.py` and `backend/tests/test_p1c_transfer_termination_concurrency.py`  
**Last Updated:** 2026-10-02

\---

## DWR-012 — Period Pay Rows with `WorkDate = NULL` and Their Relationship to HireDate Changes Are Not Yet Defined

**Issue:**  
P1b protects concrete payroll dates, but Period Pay has no `WorkDate`. It is not yet defined whether HireDate protection should use Period.StartDate, Period.EndDate, or another rule.

**Discovered In:**  
P1b HireDate payroll-history guard design/review.

**Deferred To:**  
**P7c — Payroll-entry target-contract cleanup**, or an earlier work unit only if a concrete blocker appears and the Lead explicitly authorizes it.

**Why Deferred:**  
Inventing a rule now would add payroll product semantics that are not yet locked in the contract.

**Current State:**  
P1b protects only:

* non-Void DraftLines with `WorkDate IS NOT NULL`;
* FinalLines with `WorkDate IS NOT NULL`.

No synthetic date is inferred for Period Pay.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-013 — `EmployeeType` Is Logically Retired but the Physical Database Column Still Exists

**Issue:**  
`EmployeeType` is no longer a runtime authority, but the physical column remains in the database.

**Discovered In:**  
P1b.

**Deferred To:**  
**P8b — Catalog-proven schema cleanup.**

**Why Deferred:**  
Raw-SQL fixtures and historical references still use the column. Dropping it during P1b would create broad fixture churn with no architectural value.

**Current State:**  
P1b makes the column nullable and removes runtime read/write/filter/input/output authority.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-014 — `drivers.manage` Is Retired from Runtime but the Catalog Row Still Exists

**Issue:**  
P1b moves Workforce writes to `employees.manage` and preserves `drivers.view/edit` for read/transfer meanings, but `drivers.manage` still physically exists in the permission catalog.

**Discovered In:**  
P1b permission cutover.

**Deferred To:**  
**P8b — Catalog-proven schema/catalog cleanup.**

**Why Deferred:**  
Physical deletion requires zero-reference proof across roles, tests, migrations, and compatibility paths.

**Current State:**  
There should be no runtime authorization dependency on `drivers.manage`.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-015 — Compensation Architecture Is Still Legacy and Has Not Yet Entered the Target Refoundation

**Issue:**  
The old Rate/Pay architecture still exists, while the target Compensation model is separate and intentionally scheduled for later work.

**Discovered In:**  
Unified refoundation planning; deliberately separated from Workforce refoundation.

**Deferred To:**

* **P3a** company currency authority
* **P3b** target Compensation schema/invariants
* **P3c** target authoring/resolver
* **P4a–P4c** runtime authority + snapshots/calculation/evidence
* **P5a–P5b** Status compensation + OrdinalTier
* **P6a** transfer compensation copy

**Why Deferred:**  
Compensation must be built on stable Workforce identity rather than while Employee/Driver authority is still moving.

**Current State:**  
P3a Company currency authority is closed (PR #30). G0 is closed (G0.1–G0.6, ending with PR #45). P3b is closed (PR #46): it established a dormant target persistence model and database invariants beside the legacy compensation model. P3c made the dormant model authorable and resolvable (generic PayDefinition governance and provenance, PerUnit assignment authoring, canonical resolver) without switching any operational path. P4a (closed, PR #48) cut the definition, branch-applicability and PerUnit rate authoring authority over to the target model and retired the legacy CDPI/PayItem writers and the ordinary legacy DriverRate writers. P4b (current, migration `0085`) is the clean pre-production runtime cutover: PayrollPeriodDefinitions replace the period PayItem snapshot, ordinary DraftLines become source facts keyed by `PayrollPeriodDefinitionID`, live PerUnit money is derived by the backend through `resolve_many` and the method-owned calculation boundary, and submit/resubmit/finalize stay closed (`TARGET_PAYROLL_EVIDENCE_NOT_READY`) until P4c. Zero PayDefinitions is valid and never falls back to PayItems; development databases that contain periods must be reset before `0085` applies. This debt is not resolved by P3b, P3c or P4a: the target period snapshots, calculation and evidence (P4b–P4c), Status and OrdinalTier (P5), transfer copy (P6) and removal of the inert legacy layer remain.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-10-07

\---

## DWR-016 — Transfer Compensation Copy Is Deferred Until Target Compensation Exists

**Issue:**  
When a Driver moves to a new Branch, the product will eventually need an explicit option to copy compensation from the source Driver profile to the destination profile.

**Discovered In:**  
Transfer architecture planning.

**Deferred To:**  
**P6a — Target compensation copy on transfer.**

**Why Deferred:**  
Copying legacy DriverRates now and replacing them soon afterward would duplicate work and create another dependency on architecture that is already scheduled for retirement.

**Current State:**  
Transfers before P6a must not silently introduce new compensation-copy semantics.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-017 — Final People / Workforce / Access UI Has Not Yet Been Implemented

**Issue:**  
The final frontend still needs Employee-centered People UX, Access account/link/provision UX, and Transfer integration.

**Discovered In:**  
P1b frontend boundary review.

**Deferred To:**  
**P7a — People / Access / Transfer UI.**

**Why Deferred:**  
Backend authorities need to stabilize first. Building the final UI on top of transitional contracts would create avoidable rework.

**Current State:**  
P1b performs minimum compatibility changes only, such as using `driver\_state` in the Transfer Requests picker.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-018 — Payroll-Entry Target Contract/UI Cleanup Has Not Yet Been Performed

**Issue:**  
Payroll-entry runtime/UI still needs final cleanup after target Compensation, calculation, and evidence authority are complete.

**Discovered In:**  
Post-P6D / unified refoundation planning; intentionally remained outside P1b.

**Deferred To:**  
**P7c — Payroll-entry cleanup.**

**Why Deferred:**  
Cleaning payroll entry before target Compensation and final calculation paths are complete would cause the UI/runtime contract to be rebuilt twice.

**Current State:**  
P1b must not be expanded into a payroll-entry redesign.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-019 — Legacy Runtime Still Exists After Authority Cutovers

**Issue:**  
Compatibility routes/helpers and legacy rate/runtime plumbing will continue to exist temporarily until all staged cutovers are complete.

**Discovered In:**  
The staged refoundation strategy itself.

**Deferred To:**  
**P8a — Runtime legacy deletion.**

**Why Deferred:**  
The safe sequence is: build the replacement, prove behavior, cut authority over, then delete the old runtime. Deleting legacy before proving the replacement is risky.

**Current State:**  
A temporary compatibility surface is acceptable only if it delegates to the target owner and does not contain independent business authority.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-020 — Physical Schema/Catalog Leftovers Have Not Yet Been Deleted

**Issue:**  
Some columns, permissions, and tables may become unused after cutovers but will remain physically present for a period of time.

**Discovered In:**  
P1b and later staged refoundation planning.

**Deferred To:**  
**P8b — Catalog-proven schema cleanup.**

**Why Deferred:**  
Physical deletion must happen only after proving zero runtime/test/migration references rather than based on assumption.

**Current State:**  
Examples include `EmployeeType`, possibly `drivers.manage`, and later legacy Compensation artifacts after their runtime consumers are gone.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-021 — Final Dev/Demo Data Reset and Reseed Has Not Yet Been Performed

**Issue:**  
After the refoundation is complete, the development database may need a clean reset/reseed rather than carrying old demo rows through the final architecture.

**Discovered In:**  
Unified execution planning.

**Deferred To:**  
**P8c — Reset / reseed / closure.**

**Why Deferred:**  
Current data is disposable demo/test data and must not drive architecture. Preserving it is not a goal, but the final reset is best done after target schema/runtime stabilize.

**Current State:**  
If demo data conflicts with a correct target invariant, discard the demo data and keep the invariant. Do not embed arbitrary `DELETE`/`TRUNCATE` cleanup in schema migrations merely to preserve or repair demo rows.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

## DWR-022 — “Stops Driving but Remains Employed” / Rehire Lifecycle Is Not Defined

**Issue:**  
There are no locked semantics yet for an Employee who stops being a Driver but remains employed, or for rehire after termination while preserving payroll and Driver history.

**Discovered In:**  
Workforce architecture contract while defining historical Driver rules and termination behavior.

**Deferred To:**  
**Future explicit Workforce lifecycle work — no authorized work unit currently exists.**

**Why Deferred:**  
The feature requires explicit payroll-eligibility, Driver-profile-history, and rehire semantics. Inventing it inside P1b/P1c would introduce HR lifecycle behavior that the current refoundation does not require.

**Current State:**  
Generic PATCH operations must not perform Driver→non-Driver transitions or reactivate historical/terminated Driver profiles.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-09-29

\---

# 3\. Quick Timeline for Review

|Work Unit|Deferred Entries Expected to Be Addressed|
|-|-|
|**P1b**|DWR-001|
|**P1c / lifecycle cutover**|DWR-002 partially, DWR-010, DWR-011|
|**P2a**|DWR-007|
|**P2b**|DWR-008|
|**P2c**|DWR-006, DWR-009|
|**P3a–P5b**|DWR-015|
|**P6a**|DWR-016|
|**P7a**|DWR-005 consumer migration, DWR-017|
|**P7c**|DWR-012, DWR-018|
|**P8a**|DWR-003, DWR-004, DWR-005 final deletion, DWR-019|
|**P8b**|DWR-013, DWR-014, DWR-020|
|**P8c**|DWR-021|
|**Unscheduled future lifecycle**|DWR-022|

\---

# 4\. Temporary Compatibility Rule

Temporary compatibility is allowed only in this form:

```text
old route / old hook
        ↓
canonical target owner
        ↓
one business implementation
```

This is not acceptable:

```text
new architecture
+
old architecture
+
both independently contain business rules and DML
```

If any AI discovers that a compatibility seam has become an independent authority again, that must be treated as a regression, not as an acceptable temporary solution.

\---

# 5\. Checklist Before Closing Any Work Unit

Before an AI or Lead closes any work unit:

1. Review this file.
2. Find every entry whose `Deferred To` points to the work unit being closed.
3. If the issue is fully resolved:

   * change `Issue Resolved?` to `YES`;
   * fill in `Resolved In`;
   * fill in `Resolution Evidence`;
   * update `Last Updated`.
4. If only part of the issue is resolved:

   * leave `NOT YET`;
   * update `Current State`.
5. If a new deferred item appeared:

   * add a new DWR entry.
6. Never delete resolved entries.

This keeps the file as a **living Deferred Work Register** instead of a one-time note that gets forgotten.

