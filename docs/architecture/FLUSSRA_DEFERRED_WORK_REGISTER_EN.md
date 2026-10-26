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
* `DURABLE\_DRIVER\_IDENTITY\_DRIVER\_OWNED\_COMPENSATION\_PLAN.md` (completed Durable Driver Stages 1–3 and their implemented contracts)

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

**P4b:** CLOSED / merged (PR #49; `main` at `85644f599e09c958a366c08554978010d2738434`; Target Period Definition Runtime + PerUnit Cutover; migration `0085`).

**Branch timezone authority:** CLOSED / merged (PR #56; `main` at `8da533a5310485a790eac3da6a5ec040a60381ae`; migration `0086`).

**Durable Driver Stage 1:** CLOSED / merged (DD1, PR #57, migration `0087`; permanent Driver + DriverAssignments).

**Durable Driver Stage 2:** CLOSED / merged (DD2, PR #58, migration `0088`; Driver-owned compensation; P6a is retired).

**Durable Driver Stage 3:** CLOSED / merged (DD3, PR #59, no migration; payable rounding and proration).

**P4c, P5a, P5b:** CLOSED / merged (PR #62, migration `0089`; PR #63, migration `0090`; PR #64, migration `0091`).

**P7a, P7b, P7c:** CLOSED / merged (PRs #65, #66, #67; no migrations).

**P8a:** CLOSED / merged (PR #68, migration `0092`; runtime legacy deletion).

**P8b:** CLOSED / merged (PR #69 at `30d538a9686fcc561dde2ace5533560e4345abfe`, migration `0093`; physical authority cutover).

**P8c:** CLOSED (no migration; merge pending): development reset, canonical demo seed and final refoundation closure from baseline `30d538a9686fcc561dde2ace5533560e4345abfe`.

**Current baseline:** `main @ 30d538a9686fcc561dde2ace5533560e4345abfe` (PR #69), Alembic head `0093`.

**Next work unit:** the refoundation program is complete. C2 Import is the next program; it is unblocked and not started.

**Post-G0 accepted baseline:** `main` at the PR #45 merge, `f2937bfdcbe1266a00716905a8712f3fe29baff2`. P3b merge baseline: `main` at the PR #46 merge, `edcf871f59d0616e194a8e752d9b6ba23f42b8a2`. P3c merge baseline: `main` at the PR #47 merge, `3f389499bbb1a0a70935360f32230f8e0aae4a7c`.

**Test Hygiene:** PR #29 merged after P2c.

**Migration heads:** pre-P3b `0081`; P3b `0082`; P3c `0083`; P4a advanced it to `0084`; P4b to `0085`; the Branch timezone authority to `0086`; DD1 to `0087`; DD2 to `0088`; DD3 adds no migration; P4c `0089`; P5a `0090`; P5b `0091`; P8a `0092`; P8b `0093`; P7a–P7c and P8c add no migration.

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

Update 2026-10-23 (P8c reconciliation): Durable Driver Stage 1 moved `DriverStatus` to the permanent Driver and limited it to `Active`, `Inactive` and `OnLeave`, so status no longer encodes `Transferred` or `Terminated` lifecycle, and P8a removed the compatibility readers. Every remaining runtime read selects the assignment through `core.fn_EffectiveDriverAssignment` or an effective-range overlap first and uses `DriverStatus` only as an operational filter (`EndReason IS NOT NULL OR DriverStatus = 'Active'`) in payroll eligibility, dashboard and Branch summary counts, or as a displayed Driver attribute in Workforce and roster reads. No read uses it to decide which assignment is current.

**Issue Resolved?:** YES

**Resolved In:** Durable Driver Stage 1 and P8a; reconciled and closed by P8c.
**Resolution Evidence:** `app/payroll/eligibility.py`, `app/dashboard/service.py`, `app/settings/branch_reads.py`, `app/core/service.py`, `app/workforce/effective.py` (effective-assignment selection before the status filter); `ck_Drivers_Status`; the P8c section of the Unified Plan.
**Last Updated:** 2026-10-23

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
P8a removed the mounted route after the consumer audit; Driver creation is through Workforce.

**Issue Resolved?:** YES

**Resolved In:** P8a runtime cutover (baseline `0fd86c76ee28c724270514728cdab7dcf1bba599`).
**Resolution Evidence:** `POST /core/drivers` is not mounted; test setup creates Drivers through `/workforce/employees`.
**Last Updated:** 2026-10-22

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
P8a removed the mounted route after the consumer audit; Driver profile changes are owned by Workforce.

**Issue Resolved?:** YES

**Resolved In:** P8a runtime cutover (baseline `0fd86c76ee28c724270514728cdab7dcf1bba599`).
**Resolution Evidence:** `PATCH /core/drivers/{id}` is not mounted; mutations use Workforce.
**Last Updated:** 2026-10-22

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
The route was removed after P7a migrated its consumers and the P8a consumer audit confirmed no remaining runtime callers.

Update 2026-10-22: P7a migrated frontend consumers to Workforce, Access, and transfer-authorized reads. P8a removed `/core/people` after confirming the source tree has no target caller.

**Issue Resolved?:** YES

**Resolved In:** P8a runtime cutover (baseline `0fd86c76ee28c724270514728cdab7dcf1bba599`).
**Resolution Evidence:** `/core/people` is not mounted; Workforce and transfer reads remain the target surfaces.
**Last Updated:** 2026-10-22

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
**Future explicit product decision — no authorized work unit currently exists.** (Originally P7c — Payroll-entry target-contract cleanup; see Current State.)

**Why Deferred:**  
Inventing a rule now would add payroll product semantics that are not yet locked in the contract.

**Current State:**  
P1b protects only:

* non-Void DraftLines with `WorkDate IS NOT NULL`;
* FinalLines with `WorkDate IS NOT NULL`.

No synthetic date is inferred for Period Pay.

Update 2026-10-22: P7c is a frontend-only cleanup and does not define a HireDate rule for Period Pay rows; this entry stays open for an explicit product decision.

Update 2026-10-23 (P8c): the target changed from P7c to a future explicit product decision because no refoundation work unit defines this payroll semantic. P8c closed the refoundation without deciding it. The generic Period Pay writer was retired in G0.4; how HireDate protection treats payroll facts without a WorkDate remains undecided.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-10-23

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
P1b removed runtime read/write/filter/input/output authority. P8b migration `0093` drops the physical column; affected current-schema fixtures use Workforce identity without EmployeeType.

**Issue Resolved?:** YES

**Resolved In:** P8b physical authority cutover, migration `0093` (merged via PR #69).
**Resolution Evidence:** `0093_target_physical_authorities.sql`; `test_target_physical_authorities.py`; focused Workforce/Employee tests.
**Last Updated:** 2026-10-23

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
P8b migration `0093` deletes `drivers.manage` and its obsolete grants/overrides. Current Workforce permissions and Driver read/transfer permissions remain. Current fixtures no longer recreate the retired permission.

**Issue Resolved?:** YES

**Resolved In:** P8b physical authority cutover, migration `0093` (merged via PR #69).
**Resolution Evidence:** `0093_target_physical_authorities.sql`; `test_permission_catalog.py`; CompanyRole authorization and physical-authority tests.
**Last Updated:** 2026-10-23

\---

## DWR-015 — Legacy Compensation Runtime and Schema Cleanup Remain

**Issue:**  
The target Compensation authority is operational, while pre-cutover Rate/Pay tables and runtime artifacts remain for catalog-proven cleanup.

**Discovered In:**  
Unified refoundation planning; deliberately separated from Workforce refoundation.

**Deferred To:**

* **P3a** company currency authority
* **P3b** target Compensation schema/invariants
* **P3c** target authoring/resolver
* **P4a–P4c** runtime authority + snapshots/calculation/evidence
* **P5a–P5b** Status compensation + OrdinalTier
* **P6a** transfer compensation copy — RETIRED (2026-10-18); superseded by Durable Driver Stages 1–3, which precede P4c
* **P7b** final Compensation / Settings UI cutover (closed, PR #66)
* **P7c** payroll-entry and finalized-payroll frontend cutover (closed via PR #67 at `0fd86c76ee28c724270514728cdab7dcf1bba599`)
* **P8a** runtime compatibility/authority deletion (implemented with migration `0092`)
* **P8b** physical schema/catalog cleanup (migration `0093`)

**Why Deferred:**  
Compensation must be built on stable Workforce identity rather than while Employee/Driver authority is still moving.

**Current State:**  
P3a Company currency authority is closed (PR #30). G0 is closed (G0.1–G0.6, ending with PR #45). P3b is closed (PR #46): it established a dormant target persistence model and database invariants beside the legacy compensation model. P3c made the dormant model authorable and resolvable (generic PayDefinition governance and provenance, PerUnit assignment authoring, canonical resolver) without switching any operational path. P4a (closed, PR #48) cut the definition, branch-applicability and PerUnit rate authoring authority over to the target model and retired the legacy CDPI/PayItem writers and the ordinary legacy DriverRate writers. P4b (closed, PR #49, migration `0085`) is the clean pre-production runtime cutover: PayrollPeriodDefinitions replace the period PayItem snapshot, ordinary DraftLines become source facts keyed by `PayrollPeriodDefinitionID`, live PerUnit money is derived by the backend through `resolve_many` and the method-owned calculation boundary, and submit/resubmit/finalize stay closed (`TARGET_PAYROLL_EVIDENCE_NOT_READY`) until P4c. Zero PayDefinitions is valid and never falls back to PayItems; development databases that contain periods must be reset before `0085` applies. This debt is not resolved by P3b, P3c or P4a: the target period snapshots, calculation and evidence (P4b–P4c), Status and OrdinalTier (P5), transfer copy (P6, since retired) and removal of the inert legacy layer remain. Update 2026-10-18: Durable Driver Stages 1–3 (permanent Driver and DriverAssignments, Driver-owned compensation, payable rounding and proration) now precede P4c, and P6a is retired because compensation follows the Driver.

Update 2026-10-22: P4c and P5a–P5b are closed; target calculation, Status, OrdinalTier and Driver-owned schedules are live. P7a merged via PR #65 at `b4474728196b23cf2b95aad6a30abb4914e9ff05`; P7b merged via PR #66 at `a827a2d942594b339f9fd13441d7a1a835670bce`; P7c closed via PR #67 at baseline `0fd86c76ee28c724270514728cdab7dcf1bba599`. P8a removes remaining operational compatibility; P8b owns physical schema/catalog cleanup.

Update 2026-10-23: P8a is merged via PR #68 at `a9caa739330d81bd421dd845438b27ebcb2631eb`, head `0092`. P8b migration `0093` retires the PayItem/RateType/DriverRate family and CDPI-specific storage after catalog and consumer proof; generic governance/provenance and target immutable calculation evidence remain. Branch applicability is now `BranchPayDefinitionConfig`, with SourceBranchConfigID provenance intact. See the Unified Plan P8b physical inventory. P8c remains separate.

**Issue Resolved?:** YES

**Resolved In:** P8b physical authority cutover, migration `0093` (merged via PR #69).
**Resolution Evidence:** `0093_target_physical_authorities.sql`; focused governance, branch applicability, PerUnit, OrdinalTier, schedule and immutable calculation/finalization tests; schema-authority comparison: `NO_APPLICATION_SCHEMA_DIFFERENCES`.
**Last Updated:** 2026-10-23

\---

## DWR-016 — Transfer Compensation Copy Is Deferred Until Target Compensation Exists

**Issue:**  
Original proposal (superseded): when a Driver moves to a new Branch, offer an explicit option to copy compensation from the source Driver profile to the destination profile.

**Discovered In:**  
Transfer architecture planning.

**Deferred To:**  
**SUPERSEDED (2026-10-18) — Durable Driver Stage 2 (Driver-owned compensation).** Originally P6a — Target compensation copy on transfer, now retired.

**Why Deferred:**  
Copying legacy DriverRates now and replacing them soon afterward would duplicate work and create another dependency on architecture that is already scheduled for retirement.

**Current State:**  
Resolved by DD2 / Driver-owned compensation (PR #58, migration `0088`). Ordinary rate schedules and Min/Max belong to the permanent Driver and continue across a permanent transfer without a copy. The transfer-copy design and P6a are retired; the legacy copy route, service and contracts are removed. Assignment-owned Status rates remain intentional until P5a, which makes Status rate values Driver-owned against Company-owned Status rate columns.

Historical updates (preserved): transfers before the supersession must not silently introduce new compensation-copy semantics.

Update 2026-10-18: The copy design is superseded and P6a is retired. Compensation will be owned by the permanent Driver (Durable Driver Stage 2), so a permanent transfer carries rates and Min/Max automatically; the transfer copy, its Yes/No choice and the legacy copy route are retired. Durable Driver Stage 1 introduces the permanent Driver and DriverAssignments and adds no compensation-copy semantics. The underlying need (a transferred Driver keeps its compensation) is delivered by Stage 2, so this entry stays NOT YET until Stage 2 is accepted.

Update 2026-10-19: Durable Driver Stage 1 is merged (PR #57, migration `0087`) and Stage 2 (DD2) is the current work unit, with its decisions recorded in the Durable Driver plan (Stage 2 implementation decisions). Stage 2 moves ordinary rate schedules and Min/Max rules to the permanent Driver (`DriverRateSchedules`, `DriverRateScheduleValues`, `DriverPayRules`, migration `0088`), so no transfer-time copy exists, and deletes the legacy copy route. The Stage 2 implementation is not yet reviewed or accepted by the Lead, so this entry stays NOT YET; it becomes YES only when Stage 2 is accepted under the rules above.

**Issue Resolved?:** YES

**Resolved In:** DD2 — Driver-owned compensation (supersedes P6a)

**Resolution Evidence:** PR #58, merge `95d5833737caa48cc00dc4b2c41637c2f1d7afc5`, migration `0088`; `backend/app/compensation/schedules.py`, `backend/app/payroll/driver_pay_rules.py`; `backend/tests/test_driver_owned_rate_schedules.py`, `backend/tests/test_driver_owned_pay_rules.py`, and the copy-route absence assertion in `backend/tests/test_payroll_route_contracts.py`

**Last Updated:** 2026-10-20

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

Update 2026-10-22: P7a implements the final routed People & Access hub (Workforce, Access, Transfer Requests), retires the User-centered wizard, and keeps the three areas' authorities separate. It merged via PR #65 at `b4474728196b23cf2b95aad6a30abb4914e9ff05` with no migration.

**Issue Resolved?:** YES

**Resolved In:** P7a — People / Access / Transfer UI

**Resolution Evidence:** PR #65 merge commit `b4474728196b23cf2b95aad6a30abb4914e9ff05`; routed Workforce, Access and Transfer Requests UI
**Last Updated:** 2026-10-22

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

Update 2026-10-22: P7c closed via PR #67 at baseline `0fd86c76ee28c724270514728cdab7dcf1bba599`. The Day Grid is the single operational entry UI and the Finalized Payroll Library is the finalized presentation. P8a removes the raw FinalLines API while retaining FinalLines storage/provenance, the approved immutable snapshot, and the Library read model.

**Issue Resolved?:** YES

**Resolved In:** P7c (PR #67) and P8a runtime cutover.
**Resolution Evidence:** Frontend no longer consumes the raw endpoint, which is unmounted in P8a; finalization and the Library continue to use immutable snapshot-backed FinalLines.
**Last Updated:** 2026-10-22

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
P8a removed the agreed operational compatibility routes, global-role authorization paths, pseudo-line Status/note behavior, raw FinalLines read path, period-wide Drivers Off path, and Bonus v1 replay. P8b migration `0093` now removes the remaining physical catalog artifacts; the runtime cutover itself remains P8a history.

**Issue Resolved?:** YES

**Resolved In:** P8a runtime cutover (baseline `0fd86c76ee28c724270514728cdab7dcf1bba599`).
**Resolution Evidence:** Mounted-route/runtime consumer audit; CompanyRole-only migration `0092`; canonical EntryState-only Status/note runtime; Bonus v2-only replay.
**Last Updated:** 2026-10-22

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
P8b migration `0093` implements the catalog-proven physical cutover: CompanyRole-only role assignment, retired permission/EmployeeType removal, predecessor Compensation/CDPI removal, in-place `BranchPayDefinitionConfig` rename, source/final predecessor-column removal, target period-audit identity and generic AuditLog append-only triggers. Target FinalLines, SourceSnapshot, NeedsManagerReview and immutable schedule/calculation evidence remain. Every DROP uses RESTRICT; an unexplained external FK aborts atomically. Historical migrations remain unchanged. Full inventory and retained-target rationale are in the Unified Plan P8b section. P8c reset/reseed tooling and final program closure are excluded.

**Issue Resolved?:** YES

**Resolved In:** P8b physical authority cutover, migration `0093` (merged via PR #69).
**Resolution Evidence:** `0093_target_physical_authorities.sql`; `test_target_physical_authorities.py` (fresh/non-empty upgrade, unknown-dependency rollback, identity and append-only audit); schema guards; focused financial/audit tests; one Alembic head `0093`; schema-authority comparison: `NO_APPLICATION_SCHEMA_DIFFERENCES`; frontend build/lint; `git diff --check`.
**Last Updated:** 2026-10-23

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

Update 2026-10-23 (P8c): `backend/scripts/reset_dev_database.py` is the one destructive development reset (authorize → drop the Flussra application schemas → `alembic upgrade head` → single-head assertion → startup schema guard → canonical seed → read-only verification), and `backend/scripts/canonical_demo.py` is the one canonical, payroll-ready target demo with no payroll history. No migration deletes or converts data; the partial `ensure_dev_admin.py` bootstrap is removed. The development database was reset from `0091` to `0093`, reseeded and verified.

Update 2026-10-23 (P8c review resolution): after the independent review found the in-place schema reset unsafe, the reset was changed to replace the whole disposable development database. It drops the database named in `DATABASE_URL` and creates it again from `template0` through a separately supplied lifecycle connection to a control database on the same server, then migrates, checks the schema, seeds and verifies. The lifecycle role owns the database. The application role owns every Flussra object and holds only `CONNECT`, `TEMPORARY` and `CREATE` on the database plus `USAGE` and `CREATE` on `public`, so it cannot drop or create databases. Before the DROP the reset requires all of these: an explicit development runtime; `--admin-url`; a confirmation of the database the application actually connects to, resolved by SQLAlchemy's asyncpg dialect from `DATABASE_URL` and reported back by the application connection; a control database other than the target; a lifecycle role able to create databases; the same `pg_catalog.pg_control_system().system_identifier` on the lifecycle and application connections, read under the search path `pg_catalog, pg_temp` with qualified system names, with network location not a gate; an expected owner; and no other connected session (never terminated, never `FORCE`). A failure after the DROP is reported as an absent or incomplete database, and running the reset again rebuilds it. The in-place machinery (dependency traversal, schema CASCADE, database reset settings and their provisioning tool, the exclusive connection window) is removed. `payroll_v3` keeps its verified canonical demo; its next reset needs a lifecycle credential for its server.

**Issue Resolved?:** YES

**Resolved In:** P8c — clean reset / canonical reseed / final closure (no migration).
**Resolution Evidence:** `backend/scripts/reset_dev_database.py`; `backend/scripts/canonical_demo.py`; `backend/tests/test_canonical_demo_seed.py` (fresh upgrade to `0093`, seed, verification, payroll workflow through the Finalized Payroll Library, whole-database reset back to the canonical state); `backend/tests/test_development_database_lifecycle.py` (creation of an absent database, privilege model, every check before DROP including another server and a foreign owner, open-session refusal and retry, nothing old surviving recreation, failure reporting and recovery after DROP); development database `--verify-only` after the final reset; canonical full validation before the reset redesign and focused verification after it; the P8c section of the Unified Plan.
**Last Updated:** 2026-10-23

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

Update 2026-10-18: Durable Driver Stage 1 makes the Driver permanent (0..1 per Employee, created with its first DriverAssignment), so a driver who stops driving and returns later reuses the same Driver with a new DriverAssignment. The rehire/return flow itself, the Driver→non-Driver transition and their payroll-eligibility semantics remain undefined and unbuilt.

Update 2026-10-23: the refoundation closed with P8c without defining this lifecycle; it remains future Workforce work.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-10-23

\---

## DWR-023 — External Driver Identifiers and Aliases Are Deferred to C2 Import

**Issue:**  
Durable Driver Stage 1 drops `ExternalDriverID` (no defined meaning, no UI consumer) and `core.DriverAliases` (no application consumer). Mapping external or source-system driver identities to a Driver still needs a designed home.

**Discovered In:**  
Durable Driver Stage 1 identity design.

**Deferred To:**  
**C2 Import design** (source system + external key + Driver).

**Why Deferred:**  
The meaning of an external driver identity depends on the import contract. Keeping an undefined column or an unused alias table would preserve ambiguity.

**Current State:**  
Neither dormant alias structure exists after Durable Driver Stage 1. Merged A1
(`337e3f7`, PR #74, migration `0094`) establishes safe File ingress and retires
the dormant predecessor. The [C2 refoundation design](FLUSSRA_C2_IMPORT_DESIGN.md)
replaces disposable single-file/cell staging with Import batches, source units,
layout-specific mappings and compact normalized records. It retains
Company-scoped permanent DriverNumber/EmployeeKey resolution before target
Branch/date eligibility. Known but ineligible Drivers remain known; unique
roster-name matches are proposals only. Multiple files/layouts do not create
new identity authorities. Per-import matches are staging under finalization
retention. Public confirmations/choices remain Slice B.

The unmerged A2 branch is correctness/performance evidence only. Its existence,
or the corrected schema alone, does not deliver saved external aliases.
Remembering source-system + external-key -> Driver across Imports remains the
separate later capability. This entry stays NOT YET until that capability has
its own implementation and verification.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-10-25

\---

## DWR-024 — Temporary Cross-Branch Work / Worked-for-Branch Feature Is Not Built

**Issue:**  
A Driver temporarily working for another Branch has no record or eligibility path. The architecture only fixes the rule that temporary cross-branch work is not a home DriverAssignment and never changes the Driver's home Branch or Min/Max home pay calendar.

**Discovered In:**  
Durable Driver identity plan (permanent transfer versus temporary cross-branch work semantics).

**Deferred To:**  
**Future explicit product definition — no authorized work unit currently exists.**

**Why Deferred:**  
A concrete cross-branch work record, eligibility path and UI need their own design. Modelling temporary cross-branch work as a permanent transfer would corrupt the home Branch and the Min/Max calendar.

**Current State:**  
Documentation-only rule. Evidence design (P4c) must not make separate home-Branch and host/work-Branch provenance impossible.

Update 2026-10-19: the terminology is now "temporary cross-branch work"; the meaning is unchanged. "Worked-for-Branch" remains the name of the future record. Durable Driver Stage 2 builds nothing for it: Driver-owned compensation, and the Min/Max home pay calendar, do not change because a Driver works for another Branch.

Update 2026-10-23: the refoundation closed with P8c without building this feature; it remains a future product definition.

**Issue Resolved?:** NOT YET

**Resolved In:** —  
**Resolution Evidence:** —  
**Last Updated:** 2026-10-23

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
|**Durable Driver Stage 2** (P6a retired)|DWR-016|
|**P7a**|DWR-005 consumer migration, DWR-017|
|**P7c**|DWR-018|
|**P8a**|DWR-003, DWR-004, DWR-005 final deletion, DWR-019|
|**P8b**|DWR-013, DWR-014, DWR-020|
|**P8c**|DWR-021; DWR-002 reconciliation|
|**Unscheduled future payroll product decision**|DWR-012 (moved from P7c)|
|**Unscheduled future lifecycle**|DWR-022|
|**Unscheduled future temporary cross-branch work feature**|DWR-024|
|**C2 Import design**|DWR-023|

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

