# PEOPLE / WORKFORCE / ACCESS ARCHITECTURE CONTRACT

**Status:** LOCKED — approved architecture boundary for the People / Workforce / Access refoundation; **amended 2026-10-18** (Driver identity and Scheduled transfer lifecycle, see the Driver identity amendment below)  
**Scope:** Flussra People, Employee, Driver, User, Role, Scope, Driver Transfer, and future Import boundaries  
**Precondition:** Payroll Setup is closed and remains out of scope unless a concrete dependency is later proven  
**Execution sequencing:** governed by the Unified Refoundation Execution Plan; unresolved implementation mechanisms remain for Lead design
**Changes:** changing a locked architecture decision requires an explicit amendment to this contract  
**Architecture amendment (2026-10-02):** The former DRIVER `OwnDriverDataOnly` branch-projection design is superseded by generic `Self` scope semantics (§6.3, §7). The old representation is transitional legacy state to migrate/retire in P2b.<br>
**Next execution artifact:** `FLUSSRA_UNIFIED_REFOUNDATION_EXECUTION_PLAN.md`; the older People master plan is a detailed reference only where consistent with this contract and the Unified Plan.

**Locked temporal authority amendment (2026-10-18):** This amendment supersedes every earlier Company-timezone/current-date rule and Driver assignment `EffectiveFrom`/`EffectiveTo` DATE rule in this contract and its older plans. `Branch.TimeZoneId` is the sole business timezone authority. Driver assignments use aware TIMESTAMPTZ half-open ranges `[EffectiveFromAt, EffectiveToAt)`, and a transfer switches current identity at one exact `EffectiveAt` instant. Immediate transfers resolve that instant once at administrative completion using database `NOW()`; explicitly scheduled instants must still be future when completed. Current Driver identity, Employee Branch projection, and Driver Self access all resolve against the same instant. Payroll remains explicit `BranchID + WorkDate`; an assignment overlapping any portion of that Branch-local calendar day permits activity on that WorkDate. A transfer never creates, splits, or prorates pay. Compensation, rates, Payroll Setup versions, periods, hire/termination dates, Status, Bonus, daily entries, and other business-date inputs remain DATE-authoritative; audit/event timestamps and system metadata remain instants. Captured period eligibility stores immutable eligible WorkDates and the captured Branch timezone, while live assignment coverage can restrict new inputs. No Company timezone default or runtime Company clock exists.

**Locked Driver identity amendment (2026-10-18):** This amendment supersedes every earlier reading of "Driver profile" as the Driver's identity. A `Driver` is the permanent person-driver identity: 0..1 per Employee, same Company, with an immutable Employee link. It owns the driver number, the CDL number and the durable Driver work status. A `DriverAssignment` is one continuous home-Branch tenure of that Driver over a half-open instant range `[EffectiveFromAt, EffectiveToAt)`. Wherever the older plans that reference this contract say "Driver profile" they mean a DriverAssignment, and "current Driver profile" means the Driver's effective DriverAssignment. `DriverID` means only the permanent Driver; branch payroll/work provenance is keyed by `DriverAssignmentID`. A permanent transfer closes one home DriverAssignment and opens another under the same Driver; a temporary loan is not a DriverAssignment and never changes the home Branch. Driver compensation follows the Driver (Durable Driver Stage 2), so the transfer rate-copy choice is retired. Future-dated transfers follow the `Scheduled` lifecycle (§8.3, §8.7). Implementation sequencing is recorded in `DURABLE_DRIVER_IDENTITY_DRIVER_OWNED_COMPENSATION_PLAN.md`.

---

## 1. Purpose

This document locks the domain boundaries that the People / Workforce / Access refoundation must preserve.

It is intentionally smaller than an implementation plan.

It defines:

- what an Employee is;
- what a Driver is;
- what a User is;
- how workforce identity differs from access identity;
- what access roles are allowed to do;
- how Driver transfer must preserve history;
- how future Driver self-service is bounded;
- what the future Import architecture may and may not create;
- which current behaviours are explicitly retired.

Execution sequencing is governed by the Unified Refoundation Execution Plan. Implementation design, migrations, endpoint and UI details, and test phasing must follow that plan and this contract.

The contract owns semantics and invariants. It does not select an implementation mechanism unless it explicitly requires one; the Lead records any remaining architectural mechanism decision before implementation.

---

## 2. Canonical Domain Model

The canonical model is:

```text
Company
│
├── Workforce
│   │
│   └── Employee
│       │
│       ├── non-Driver employee
│       │
│       └── Driver (permanent identity, 0..1 per Employee)
│           ├── driver number, CDL number, Driver status
│           ├── Driver compensation (rates, Min/Max)
│           └── DriverAssignment(s)
│               ├── one continuous home-Branch tenure
│               ├── effective-instant window
│               ├── historical lineage
│               └── Payroll history
│
└── Access
    │
    └── User (application login account)
        ├── optional Employee link (0..1 ↔ 0..1)
        ├── Role + Scope assignment
        └── Permission overrides
```

Workforce and Access are related domains, but neither silently creates nor mutates the other. A linked DRIVER User derives self identity from the explicit User–Employee link, the Employee’s Driver and that Driver’s effective DriverAssignment (§7.5). A Driver transfer or termination changes effective Workforce identity over time; it does not mutate an Access Self assignment or create Access branch membership (§7.6, §8.5).

### 2.1 Effective-time vocabulary

These terms are used throughout this contract:

- A DriverAssignment is **effective at instant T** when `EffectiveFromAt <= T < EffectiveToAt` (half-open; a NULL `EffectiveToAt` is open). Business-date payroll does not cast an instant to a DATE; it uses the Branch-local WorkDate overlap rule of the Branch Timezone Authority contract.
- The **current DriverAssignment** of a Driver (and so of its Employee) is the assignment effective at the database transaction instant `NOW()`.
- A **historical DriverAssignment** is an assignment whose window has ended (`EffectiveToAt <= NOW()`).
- A **pending DriverAssignment** is an assignment whose `EffectiveFromAt` is in the future; it is not current.

Throughout this contract, "current" always means current and effective in this sense.

"Current" is never determined by status alone. The assignment window decides which assignment applies at an instant; the window and the end reason (§4.6) are written together and must agree.

---

## 3. Employee Contract

### 3.1 Canonical workforce identity

`core.Employees` is the canonical workforce-person domain.

`EmployeeID` is the durable workforce identity.

An Employee represents the person who works for the company, not their login account and not one particular Driver assignment.

### 3.2 Employee lifecycle

The product must support Employees independently of Driver and User records.

A supported Employee workflow must allow at least:

- create Employee;
- update Employee-owned fields;
- represent the Employee's branch according to §3.5;
- represent non-Driver Employees;
- represent employment status;
- preserve a stable Employee identity across Driver transfers.

The Employee domain is limited to what Flussra needs for workforce identity, branch ownership, basic employment state, Driver specialization, future Import, and Access linking (§18).

### 3.3 Employee types

Non-Driver Employees are supported now.

The product must not assume:

```text
Employee == Driver
```

Whether an Employee is a Driver is determined only by the existence of a permanent Driver for that Employee; whether that Driver currently has a home Branch is determined only by its current DriverAssignment.

`EmployeeType`, if retained, is workforce metadata only:

- it never grants Access;
- it never determines authorization;
- it is not the authority for whether an Employee is a Driver and must not contradict Driver truth;
- Access-role names (for example Payroll Manager) are not Employee types.

The current free-text values (`Driver`, `OfficeStaff`, `Manager`, `PayrollUser`) are not canonized by this contract. Whether `EmployeeType` is retired, renamed, repurposed, or replaced with another small classification is decided by the refoundation master plan based on actual product need.

### 3.4 Employee keys

`EmployeeKey` is the workforce-owned, company-unique, stable Employee identifier.

It:

- is never derived from a `UserID` or any other Access data;
- does not change because a Driver transfers branches.

The existing convention that can create:

```text
EMP-{UserID}
```

as a side effect of access-role assignment is retired.

Workforce identity is owned by the workforce domain.

External source-system identifiers, aliases, and import mapping identifiers belong to C2 Import. A future C2 mapping may resolve an external identifier to exactly one Employee.

### 3.5 Employee branch authority

`Employee.BranchID` is workforce data only. It never grants, restricts, or moves Access.

For an Employee with **no** current or pending DriverAssignment, `Employee.BranchID` is the canonical current employment/home branch and is editable through the Employee workflow.

For an Employee **with** a current or pending DriverAssignment:

- the current DriverAssignment (or, if none is yet current, the pending one) is authoritative for the Driver branch;
- `Employee.BranchID` is a synchronized workforce projection of that assignment's branch;
- it is not independently editable;
- it changes only as a consequence of Driver creation with its first assignment or of a Driver transfer becoming effective (§8.3).

The first Driver for an existing Employee is created, with its first DriverAssignment, in the Employee's current `Employee.BranchID`. Any later branch change for a Driver is a Driver transfer.

---

## 4. Driver Contract

### 4.1 Driver is the permanent identity; DriverAssignment is the home-Branch tenure

`core.Drivers` represents the permanent Driver: the driver identity of an Employee. A Driver is not a second person; the Employee remains the canonical human identity.

`core.DriverAssignments` represents one continuous home-Branch tenure of a Driver.

The relationship is:

```text
Employee
   └── Driver                    0..1 per Employee, permanent
          └── DriverAssignment[]     home-Branch tenures over time
```

A Driver owns the driver number (stable; unique per Company when present; never generated per assignment), the CDL number (a scalar current value), and the Driver's durable work status (§4.6). A permanent transfer never creates a new Driver.

A Driver is created together with its first DriverAssignment in one Workforce operation (§11.1). A driver who stops driving and later returns reuses the same Driver with a new assignment; the lifecycle that permits it is not defined here (§4.6 item 7).

### 4.2 DriverID and DriverAssignmentID semantics

`DriverID` identifies only the permanent Driver. It is used for driver identity and display (driver number, CDL number, name through the Employee), Self ownership resolution (§7.5), the transfer request subject (§8), import matching (§9.3), and Driver-owned compensation (§8.4).

`DriverAssignmentID` identifies one home-Branch tenure and is the branch/effective payroll-history key. Drafts, day entries, eligibility, bonus events, calculation snapshots, final lines and evidence reference it. Evidence and final records also carry the permanent `DriverID` and the driver number snapshot, bound to the assignment so the two cannot disagree.

Existing payroll history must not be collapsed into Employee history, into the Driver, or into another assignment.

### 4.3 Branch and effective-time semantics

A DriverAssignment is:

- company-bound;
- branch-bound;
- effective over a half-open instant range (§2.1);
- historically preserved.

A historical DriverAssignment must remain available for payroll provenance.

Invariants:

1. A Driver, its Employee, and each of the Driver's DriverAssignments always belong to the same company.
2. An Employee has at most one Driver.
3. For one Driver, DriverAssignment windows must not overlap.
4. At any instant, a Driver (and therefore its Employee) has at most one effective DriverAssignment.

### 4.4 Branch immutability

`DriverAssignments.BranchID` is immutable after the assignment is created.

There is no history-dependent exception: an assignment's branch is never changed, whether or not payroll, rate, eligibility, entry, or snapshot records already reference it. An assignment's `DriverID` is likewise immutable, and a Driver's `CompanyID` and `EmployeeID` are immutable.

Working permanently in a different branch means a Driver transfer, which creates a new DriverAssignment of the same Driver.

Role assignment, User editing, access-scope editing, Employee editing, or general People UI actions must never move a Driver or a DriverAssignment between branches.

Assignment-branch immutability, Driver/Employee same-company ownership, one Driver per Employee, and at most one effective DriverAssignment per Driver at any instant are integrity invariants. They must be database-enforced where the database can express them; any part the database cannot express must be enforced in the owning service with tests.

### 4.5 Historical assignments

A historical (ended) DriverAssignment is never mutated into another branch or made current again.

Historical lineage and effective windows are authoritative and must remain intact. Only a future boundary may change: the source end or destination start of a `Scheduled` transfer, and only through transfer cancel, transfer reschedule or termination (§8.7, §8.8).

### 4.6 Minimum Driver / Employee status rules

This contract defines only the status semantics required to protect payroll and transfer history. It is not an HR lifecycle.

Status ownership is split:

- `Driver.DriverStatus` is one of `Active`, `Inactive`, `OnLeave`. It is the durable work state of the person-driver and survives transfers; a transfer never sets it.
- `DriverAssignment.EndReason` is `NULL`, `Transferred` or `Terminated`. It records why the assignment window ends and is written together with `EffectiveToAt`: the window is open if and only if there is no end reason.
- `Employee.EmploymentStatus` is unchanged.

Rules:

1. A `Transferred` assignment is closed by a Driver transfer. It remains effective through its `EffectiveToAt` for payroll purposes and can never become current again after that. `Transferred` is set only by the Driver transfer operation.
2. A `Terminated` assignment is never reactivated in place (§4.7). A later rehire/return, if ever supported, reuses the same Driver with a new assignment under a separately defined lifecycle rather than reopening historical state.
3. Generic Driver or Employee editing must never reactivate an ended DriverAssignment or alter its effective window.
4. End reasons and effective windows must not contradict payroll eligibility: an assignment that has ended (transferred or terminated) always has a closed window and an end reason. The only empty window permitted is a never-effective assignment retained as `Terminated` (§4.7). An assignment is lifecycle-eligible for payroll when it has an end reason (historical; its window decides) or its Driver is `Active`.
5. Driver status, end-reason and employment status values are small, closed sets enforced by the platform, not free text.
6. For Driver Employees, Employee `EmploymentStatus`, `HireDate`, and `TerminationDate` are payroll-eligibility inputs. Changing them must never rewrite locked or finalized payroll evidence.
7. Ending a Driver's assignments while the Employee remains employed in a non-Driver capacity (for example "stops driving, becomes office staff") requires payroll-eligibility semantics that are not currently defined. That transition workflow is deferred and is not part of this refoundation.

### 4.7 Terminating a Driver Employee

When a currently employed Driver Employee is terminated from employment on date D, one Workforce business operation applies:

```text
Employee:                         EmploymentStatus = Terminated, TerminationDate = D
Current DriverAssignment:         EffectiveToAt    = end of D in its Branch, EndReason = Terminated
```

Both changes succeed or fail together. Termination does not change `Driver.DriverStatus`: the Driver's work state is unaffected, and `Employee.EmploymentStatus` plus the `Terminated` assignment window govern. The terminated Employee's DriverAssignments never become current after that cap (computed by the Branch Timezone Authority contract's local-day boundary); §8.8 defines how a pending assignment and any in-flight transfer are closed.

Historical integrity:

- prior DriverAssignments remain unchanged;
- Driver compensation remains with the Driver, and historical payroll records remain on their original `DriverAssignmentID`s;
- payroll history, finalized evidence, snapshots, and used-rate evidence remain unchanged;
- termination never rewrites historical payroll.

A `Terminated` assignment is never reactivated in place.

Termination is a Workforce operation and does not itself modify Access records. A DRIVER role assignment whose linked Employee's Driver has no current DriverAssignment confers no current-assignment self-service authorization (§7.6).

This section does not cover the deferred "stops driving but remains employed" transition (§4.6 item 7).

---

## 5. User / Access Contract

### 5.1 User is an application login account

`sec.Users` is the authentication/access domain.

In Flussra, a User is an **application login/access account**.

A User is not:

- an employment relationship;
- a generic HR "person";
- a workforce assignment.

A User is not automatically an Employee.

### 5.2 Access is optional for workforce people

An Employee does not require a User.

Examples that must remain valid:

```text
Employee ✅
Driver ✅
User ❌
```

and:

```text
Employee ✅
Driver ❌
User ❌
```

### 5.3 Workforce membership is optional for access accounts

A User does not automatically require an Employee.

A valid product case may include an external accountant, consultant, or other authorized account that is not a workforce Employee.

### 5.4 User ↔ Employee linking

`User.EmployeeID` remains the explicit optional link between Access and Workforce.

Cardinality:

```text
User     → Employee : 0..1
Employee → User     : 0..1
```

Rules:

- a User references at most one Employee;
- an Employee is referenced by at most one User;
- the link holds regardless of the User's active state — deactivating a User does not free or reassign its Employee link;
- relinking a User or an Employee to a different counterpart requires an explicit unlink first;
- User and Employee must belong to the same company;
- linking requires explicit target Employee selection;
- linking never creates an Employee;
- linking never infers identity from username, email, display name, `UserID`, or `EmployeeKey`.

Link cardinality and same-company ownership are integrity invariants; implementation design selects enforcement details without changing these semantics.

Access-role assignment must not be the mechanism that creates this link implicitly.

### 5.5 Provisioned and staged accounts

A **login-enabled** User must be in a valid provisioned state: it holds the Role/Scope authority required to use Flussra.

A User may exist without an active Role/Scope assignment only as an explicitly **staged/unprovisioned** account:

- login is disabled;
- the staged state is explicitly represented, not inferred from a missing assignment;
- it cannot authenticate or gain access by any path until it is provisioned.

A valid provisioned Self-only assignment is authority independent of branch membership. Login and `/auth/me` must represent that authority without requiring or fabricating a branch-access row; the response shape is not fixed by this contract.

---

## 6. Access Roles Contract

### 6.1 Role meaning

An Access Role answers:

> What may this User do inside Flussra?

It does not answer:

> What is this person's employment type?

These are separate concepts even when they share similar names.

### 6.2 Role assignment side effects

Assigning, changing, or removing an access role must never:

- create an Employee;
- create a Driver or DriverAssignment;
- move an Employee to another branch;
- move a DriverAssignment to another branch;
- change EmployeeType;
- terminate or reactivate workforce records;
- create, change, or remove a User↔Employee link.

Removing or replacing the `DRIVER` role removes its `Self` assignment and leaves the User↔Employee link and all Workforce records unchanged.

The current DRIVER-role workforce side effects are explicitly retired.

### 6.3 Role and Scope remain Access concerns

Role, Scope, and permission overrides remain Access concepts with separate meanings:

- **Role** identifies the assignment's capability set.
- **Permission** identifies an action.
- **Scope** identifies the resource set on which an allowed action may apply. The supported concepts are `AllCompanyBranches`, `SpecificBranch(branch_id)`, and `Self`.
- **Identity/relationship resolution** proves whether a concrete resource belongs to `Self`.

Role and Scope form one assignment and are never saved as separate operations. Their authority stops at the Access boundary.

`Self` is not branch access and carries no `BranchID`. By itself, Self grants no branch-wide or generic operational/administrative authority. A Self-compatible permission or explicitly designed self-service operation may authorize only an own-resource action after the canonical Self relationship is proven.

Permission overrides may widen the action set only within resource scope already established by valid Access authority. An override never creates company, branch, or Self scope; never bypasses ownership matching or the DRIVER capability ceiling; and cannot make an action valid for `Self` unless that action is explicitly Self-compatible. The contract fixes these semantics without prescribing override storage or foreign keys.

`COMPANY_OWNER` remains a protected system role with dynamic permission-catalogue access only through a valid active company-wide owner assignment. A malformed branch-scoped or Self-scoped owner assignment does not grant unrestricted company authority.

---

## 7. DRIVER Access Role Contract

### 7.1 Preserve DRIVER role

The `DRIVER` Access Role is intentionally preserved.

It is future-facing functionality for Driver self-service.

It is not dead code merely because the full Driver-facing screen does not exist yet.

### 7.2 Intended meaning

The role means:

> This linked Driver may log in to see and use their own permitted Driver-facing information.

Possible future surfaces include their own:

- work/activity;
- payroll/pay information;
- relevant Driver status;
- permitted self-service data.

This contract does not implement that future screen.

### 7.3 DRIVER is self-only

```text
DRIVER  ⇔  Self
```

- For the current product, exact RoleCode `DRIVER` requires `Self`; `DRIVER + SpecificBranch` and `DRIVER + AllCompanyBranches` are invalid.
- `DRIVER` is the only role currently supported with `Self`. This does not prohibit a future role from using `Self` after explicit policy/product design.
- `Self` is generic and contains no Driver-specific meaning or branch identifier.
- The legacy `OwnDriverDataOnly` assignment is transitional state to migrate/retire; it is not the target scope.
- Driver-role identity is matched by exact role identity/code `DRIVER`, never by substring or name matching.

### 7.4 DRIVER assignment preconditions

Assigning or activating `DRIVER` requires that:

1. the User is explicitly linked to an Employee (§5.4);
2. that Employee belongs to the User's company; and
3. that Employee has a Driver with a current DriverAssignment, resolved through the canonical effective-assignment resolver.

A pending DriverAssignment is not current. A future Driver may be linked and kept staged, but `DRIVER` role assignment is rejected until the DriverAssignment becomes effective. For each current/date-sensitive Driver action, resolve the linked Employee's Driver and its DriverAssignment effective at that operation's defined instant. Missing link, company mismatch, or no effective DriverAssignment for a current action fails closed.

Assigning `DRIVER` never creates, links, moves, or modifies Employee or Driver records.

**Future Drivers.** A future Driver may be prepared before their home DriverAssignment becomes effective. Before the assignment's `EffectiveFromAt` it is valid to have:

- the Employee;
- the Driver with a pending DriverAssignment;
- an optional User account;
- an explicit User↔Employee link.

The pending DriverAssignment is not current, so DRIVER self-service must not become active and the account must not gain DRIVER self-service authorization before that instant. Access provisioned ahead of time remains an explicitly staged, login-disabled account (§5.5) until the DriverAssignment becomes effective. A pending assignment is never treated as current merely so an account can be provisioned.

### 7.5 Self-service authorization model

Three concepts are distinct:

**Own identity.** Which Driver and DriverAssignment records belong to a DRIVER User is always derived from:

```text
User.EmployeeID → Employee → Driver → effective DriverAssignment
```

It is never derived from an Access-scope `BranchID`.

**Historical reads.** "Own data" may include the linked Employee's Driver's historical DriverAssignments where a self-service feature permits history (for example historical pay or work records).

**Current actions.** A self-service action that operates on a DriverAssignment targets the linked Driver's assignment effective at the instant that operation's contract defines (normally the current instant).

A stale Access scope is never used to decide which Driver is "own".

### 7.6 Self scope is independent of branch access

The former `OwnDriverDataOnly` model stored a `BranchID` on the Access assignment and synchronized it with the current Driver branch. This Access-side branch projection is retired by this amendment and must be migrated/removed in P2b.

A `Self` assignment has `BranchID = NULL`. `Self` is not a fake branch membership, does not mean the User's current work branch, and grants no branch-wide access. Branch access views/functions must not fabricate branch membership for a Self assignment.

Self ownership is proven from the User's explicit Employee link and the relevant Employee-owned resource. For a current Driver operation, resolve the linked Employee's Driver and its DriverAssignment effective at the operation's defined instant. Historical Driver reads may include only DriverAssignments of that linked Employee's Driver and only where the endpoint's self-service contract permits history.

A missing link, company mismatch, or missing effective DriverAssignment for a current action fails closed. Transfer and termination do not update the Access Self assignment. `Employee.BranchID` remains a distinct Workforce projection under §3.5 and may still follow the effective DriverAssignment; this does not create Access branch authority.

Unrelated administrative branch scopes never follow a Workforce transfer (§8.6).

### 7.7 DRIVER capability boundary

The `DRIVER` role's effective permissions — including permissions granted to the role and any per-user permission overrides — must be limited to self-service-safe capabilities.

A DRIVER User must never obtain general administrative or mutation authority over:

- payroll;
- Driver Pay Rates;
- workforce / Employee / Driver administration;
- roles and permissions;
- other Drivers' data.

A DRIVER User must not be able to create, edit, approve, or void rates, payroll, or workforce records merely because the target belongs to themselves.

A User may have multiple active Access assignment rows; the authorization boundary must not assume that the table physically permits only one. P2b does not add mixed DRIVER/admin product behavior, and unexpected mixed rows must not bypass the DRIVER Self capability ceiling. Any deliberate mixed-mode behavior requires separate product design.

Future self-service commands (for example explicit request or acknowledgement workflows) are permitted only when separately designed as self-service operations. This contract does not design them.

Generic roster permissions such as `drivers.view` must not be the grant used to authorize a Driver's own self-service data.

### 7.8 Roster privacy

A Driver self-service User must not be able to browse the branch workforce roster or other Drivers' personal/operational information through generic People/Driver endpoints.

The self-only boundary must apply consistently before Driver self-service is exposed as a product feature.

---

## 8. Driver Transfer Contract

### 8.1 Durable Employee and Driver identity

A transfer preserves `EmployeeID` and the permanent Driver (`DriverID`, driver number, CDL number, work status).

The person does not become a new Employee, and does not become a new Driver, because they changed branches.

### 8.2 DriverAssignment transition

A permanent Driver transfer must:

1. end the source DriverAssignment's window at the transfer `EffectiveAt` instant with `EndReason = Transferred`;
2. preserve the source assignment's historical identity and lineage;
3. open a new DriverAssignment of the same Driver in the target branch;
4. start the destination assignment's window at the identical `EffectiveAt` instant;
5. preserve old payroll references on the source `DriverAssignmentID`.

No new Driver and no new driver number is created or generated.

Conceptually:

```text
Employee 153
└── Driver 40 (driver number D-0417)
    │
    ├── DriverAssignment 500 — Source Branch — [start, EffectiveAt), EndReason Transferred
    └── DriverAssignment 721 — Target Branch — [EffectiveAt, open)
```

### 8.3 Effective-instant transition and Scheduled lifecycle

A transfer must never make the destination assignment or destination branch current before its `EffectiveAt`, regardless of when the transfer was approved, completed or scheduled.

Stored request statuses are `PendingSourceApproval`, `PendingTargetApproval`, `Returned`, `Rejected`, `Approved`, `Scheduled`, `Completed` and `Cancelled`.

- **Create.** The request names the permanent Driver. The service resolves that Driver's current DriverAssignment as the source (a DRIVER User may create a request only for its own Driver). `EffectiveAt` NULL means immediate; otherwise it must be a future instant after the source assignment's start.
- **Complete an `Approved` request, immediate** (`EffectiveAt` NULL): the boundary is database `NOW()`. The source closes and the destination opens at that instant, and the request becomes `Completed`.
- **Complete an `Approved` request, future `EffectiveAt`:** the same windows are materialized at completion (the source ends, and the not-yet-effective destination starts, at `EffectiveAt`) and the request becomes `Scheduled`. It is not `Completed`.
- **Activation.** The assignment windows are the single authority, so currentness switches at `EffectiveAt` by time, exactly once, with no job. The request status is derived through one SQL function: a stored `Scheduled` request whose `EffectiveAt` has been reached is reported as `Completed`, with `EffectiveAt` as its completed instant. Every read, filter and active-transfer check uses that function.

Example: scheduled October 1 for an `EffectiveAt` of the start of November 1 in the source Branch's timezone. Until that instant the source assignment remains current, the Employee's current workforce branch is the source branch, and the request reads `Scheduled`. From that instant the destination assignment is current, `Employee.BranchID` reflects the destination branch, DRIVER self-service follows the destination assignment, and the request reads `Completed`.

Every consequence of "current" (the `Employee.BranchID` Workforce projection (§3.5), self-service current actions (§7.5), and current Workforce views) follows the assignment windows, not the approval, completion or scheduling timestamp. Self authorization follows the linked Employee, its Driver and the effective DriverAssignment; no Access branch projection is synchronized.

Scheduling, completing, cancelling and rescheduling a transfer each write a Workforce audit event.

### 8.4 Compensation follows the Driver

Ordinary Driver rates and Driver Min/Max rules are owned by the permanent Driver (Durable Driver Stage 2). A permanent transfer therefore carries compensation with the Driver automatically: there is no transfer-time copy, no Yes/No choice, and no rate or pay-rule record created for the destination assignment.

The former explicit choice to copy Driver Rates to the destination assignment, its copy semantics, and the legacy copy route are retired.

A transfer never creates, moves, copies, rebinds or rewrites rate or pay-rule records, and never rewrites historical payroll. Min/Max rules are bound to the Driver's home pay calendar; a permanent change of home Branch to an incompatible pay calendar is a payroll blocker, not an advisory notice. Status rate columns remain Branch-owned, and a transfer never requires re-entering status rates.

Until Stage 2 lands, existing rate and pay-rule records stay keyed to their DriverAssignment with unchanged ownership, and the transfer neither copies nor moves them. There is no silent carry-forward.

### 8.5 Linked DRIVER self-service User

When the transferred Employee has a linked User whose Access Role is `DRIVER`, the User–Employee identity link remains unchanged. The Self assignment is not rewritten when the transfer is approved, completed, scheduled or becomes effective. Current Driver self authorization follows the linked Employee's Driver and its effective DriverAssignment on the operation's relevant instant (§7.5); transfer never changes Access scope or creates branch membership.

### 8.6 Administrative roles remain independent

A workforce transfer must not automatically move unrelated administrative access.

For example, a Payroll Manager or Company-level administrative User may have access scope independent of the Employee's current employment branch.

For every non-DRIVER role, an Employee's branch change has no automatic effect on that User's Access.

Only access defined as Driver self-service follows the Driver transfer.

### 8.7 Cancel and reschedule

Cancel and reschedule use the same permission as complete. Whether `EffectiveAt` is still in the future is checked under lock with database time.

**Cancel.** Allowed for `PendingSourceApproval`, `PendingTargetApproval`, `Returned` and `Approved` requests, and for a `Scheduled` request only while `EffectiveAt` is still in the future. Cancelling a `Scheduled` request is refused if the destination assignment has any payroll fact (draft lines, day-entry states, bonus events, final lines, evidence) or has eligibility rows in a period that is not `Draft`, `Open` or `Returned`. Otherwise the destination assignment and its eligibility rows are deleted, the source assignment is reopened (open end, no end reason, no transfer link), and the source's eligibility row is re-captured in the `Draft`, `Open` and `Returned` periods of the source Branch that overlap the reopened interval. If a period in any other state overlaps that interval, the cancel is refused. The request becomes `Cancelled`.

**Reschedule.** A new `EffectiveAt`, which must be a future instant after the source assignment's start, may be set on a request in `PendingSourceApproval`, `PendingTargetApproval`, `Returned` or `Approved` (where it only updates the request), and on a `Scheduled` request while its current `EffectiveAt` is still in the future. For `Scheduled`, both assignment boundaries move atomically and the destination assignment keeps its identity. The reschedule is refused if any payroll fact of the source or of the destination falls on WorkDates its changed window no longer covers, or if a non-editable period of either Branch overlaps the affected interval. Both assignments' eligibility rows are re-captured in the affected `Draft`, `Open` and `Returned` periods.

This re-capture is the one narrow exception to the rule that existing eligibility rows are left untouched; it concerns only the named assignment (see the Branch Timezone Authority contract).

### 8.8 Termination and Scheduled transfers

Termination keeps the §4.7 semantics: it caps each DriverAssignment in its own Branch, and an empty future assignment is retained only as an empty `Terminated` assignment. A `Scheduled` transfer whose `EffectiveAt` is at or after the source's termination cap becomes `Cancelled`. One whose `EffectiveAt` is before the cap stays, and is effectively `Completed` at `EffectiveAt`.

### 8.9 Temporary loan is not an assignment

A temporary loan does not close, replace or create a home DriverAssignment. It does not change the Driver's home Branch, the `Employee.BranchID` projection, or the home pay calendar that Min/Max rules are bound to. A future loan feature may record separate host-Branch work provenance; that feature is not defined by this contract, which only preserves this rule.

---

## 9. Pay Rates and Payroll History Contract

### 9.1 Payroll history is keyed by DriverAssignmentID; DriverID is durable

Branch payroll history (drafts, day entries, eligibility, bonus events, calculation snapshots, final lines and evidence) is keyed to `DriverAssignmentID`. Evidence and final records also carry the permanent `DriverID` and the driver number snapshot (§4.2).

`DriverID` is durable: it never changes across transfers and never identifies a branch tenure.

Driver compensation is owned by `DriverID` from Stage 2 (§8.4). Until then, rate and pay-rule records stay keyed to their DriverAssignment with unchanged ownership.

This is intentional.

### 9.2 Never rewrite history because an Employee moved

A transfer must not rewrite old payroll, rates, finalized evidence, snapshots, or eligibility history to the destination DriverAssignmentID.

Compensation continues under the same Driver (§8.4); there is no transfer copy.

### 9.3 Import resolution rule

A future importer must resolve operational Driver identity using enough context to identify exactly one effective DriverAssignment of one Driver.

The expected conceptual resolution is:

```text
Company
+ EmployeeKey (or a C2-defined external identifier that maps to exactly one Employee)
        ↓
the Employee's Driver (0..1)
+ Branch
+ Work/Effective Date
        ↓
the Driver's DriverAssignment in that branch effective for that date
        ↓
DriverAssignmentID (with its DriverID)
```

"Effective" has the meaning defined in §2.1; for a WorkDate it is the Branch-local day overlap rule.

An import matches a durable Driver: `import.ImportRows.MatchedDriverID` refers to the permanent Driver. External identifiers and aliases are designed in C2 Import as source system + external key + Driver; this contract creates no alias or external-ID structure.

Name-only matching is not acceptable authority.

---

## 10. People & Access Product Boundary

### 10.1 Workforce authority

Workforce-facing People authority is Employee-centered.

Its primary identity is `EmployeeID`.

It may display related Driver and Access information, but a User account must not be the required primary record.

### 10.2 Access-account authority

Access-account authority is User-centered.

It owns:

- username/authentication;
- login enablement;
- password/account state;
- Role and Scope assignment;
- permission overrides;
- optional Employee link.

### 10.3 Presentation is not architecture

The UI may present Workforce and Access as tabs, pages, a joined view, or contextual actions.

A joined Employee-centered People view with explicit Access actions is compliant.

This contract does not mandate specific pages or tabs.

### 10.4 Transfer Requests

Driver Transfer Requests remain a dedicated workflow.

They must not be replaced by editing branch fields in People, User, Role, Scope, Employee, or Driver forms.

### 10.5 Current People wizard

The existing Add Person wizard is not preserved as the creation authority.

Its current model:

```text
create User
→ assign Role
→ DRIVER side effect creates workforce
→ add permissions
```

is retired.

The future workflow must not present several committed operations as if they were one unfinished wizard.

---

## 11. Creation Workflow Contract

### 11.1 Workforce transactions

Each of the following is one workforce transaction:

```text
Create non-Driver Employee
```

```text
Create Driver Employee = Create Employee + Create Driver + first DriverAssignment
```

```text
Add Driver (with its first DriverAssignment) to an existing Employee
```

These are workforce operations.

No workforce operation creates a User, provisions login access, or assigns a Role.

### 11.2 Access provisioning transactions

Access is a separate, optional operation, never part of a workforce transaction.

```text
Employee (optional)
   ↓ separate, optional
Create Access Account = User + optional Employee link + Role/Scope assignment   (one transaction)
   ↓ separate, optional
Permission overrides (independently managed)
```

A staged/unprovisioned User (§5.5) may be created only as an explicit, login-disabled state; it is not the normal Create Access Account outcome.

Linking or unlinking an existing User and Employee is its own Access operation and enforces §5.4 and, when the User holds `DRIVER`, §7.4.

### 11.3 Atomicity

When the product presents multiple writes as one business operation, those writes must have one defined transactional outcome.

The product must not knowingly leave states such as:

```text
User created, login enabled
Role missing
Employee missing
```

because the user clicked Next in a wizard.

Where a workflow intentionally consists of independent operations, the UI must present them as independent saved operations rather than pretending they are one uncommitted form.

### 11.4 Retry behaviour

Creation operations must either:

- be safely idempotent; or
- reject duplicate/conflicting retries with a clear domain conflict.

Back-navigation must not create duplicate Users or Employees.

---

## 12. Security Contract

The refoundation must preserve or strengthen:

- company isolation;
- branch authorization;
- existing permission authority;
- historical payroll constraints;
- Driver self-only access;
- protected payroll/rate foreign-key and trigger guarantees.

### 12.1 Self-service Driver restriction

Every DRIVER self-service path must resolve own identity through the link (§7.5), restrict the User to their own permitted data, and respect the capability boundary (§7.7).

### 12.2 No branch-roster leakage

Generic Workforce read endpoints must not turn `Self` or the `DRIVER` role's permissions into branch-wide workforce visibility. Self-authorized reads must prove ownership through the canonical User–Employee relationship.

### 12.3 Same-company links

A User↔Employee link must never cross company boundaries (§5.4).

---

## 13. Legacy Import Contract

The existing dormant `import.*` tables are not current runtime authority.

They must not be revived wholesale merely because they already exist.

Useful ideas such as:

- batches;
- files;
- mapping templates;
- row validation;
- validation issues and rejection reasons;

may inform future C2 Import design.

However, future Import must write through the canonical Employee / Driver / payroll domain rather than establishing a parallel source of truth.

---

## 14. Future Import Boundaries

### 14.1 Workforce Import

A workforce import may create/update:

```text
Employee
└── Driver and its first DriverAssignment when applicable
```

It must not create User login accounts by default.

### 14.2 Access Import

If bulk Access provisioning is ever required, it is a separate explicit workflow.

It must not be an implicit side effect of workforce import.

### 14.3 Driver Rate Import

Rate import must resolve the correct Driver (§9.3) before writing a Driver rate.

### 14.4 Payroll operational Import

Operational payroll import resolves existing workforce/Driver identity and feeds the canonical operational payroll model.

It must not silently create missing people as a convenience unless a later explicit product contract allows a controlled onboarding workflow.

---

## 15. Existing Development Data Strategy

All current People / Employee / Driver / User data is development/demo data and is disposable.

Therefore:

- no production-grade compatibility migration is required for existing bad links;
- no duplicate-person reconciliation framework is required;
- no preservation logic is required solely for current demo records;
- the refoundation must not accumulate complexity to protect disposable data.

The implementation may use current development data temporarily for investigation and testing.

After the new architecture is implemented and validated, development data may be reset and reseeded from a clean valid baseline.

Schema history, migration correctness, payroll invariants, and domain behaviour still matter even though current records are disposable.

---

## 16. Explicitly Retired Behaviours

The following current behaviours are not part of the target architecture:

1. People list using `UserID` as the implied workforce identity.
2. Add Person Step 1 persisting a real login-enabled User before the workflow is complete.
3. Back/resubmit creating another User.
4. DRIVER role assignment creating an Employee.
5. DRIVER role assignment creating a Driver or DriverAssignment.
6. DRIVER role assignment implicitly linking a User to an Employee.
7. Role/Scope editing moving Driver or Employee branch.
8. Any direct update of an existing DriverAssignment's branch.
9. EmployeeKey being derived from UserID.
10. A role save being capable of undoing a completed Driver transfer.
11. Driver-section visibility depending only on Access Role rather than workforce truth.
12. Requiring a password/login account merely to add a Driver to the workforce.
13. Treating `/core/people` as sufficient Employee management when it is read-only.
14. Allowing Driver self-service users general branch-roster visibility.
15. Detecting the Driver role by substring matching of role codes.
16. `DRIVER` assigned with `SpecificBranch` or `AllCompanyBranches`; granting `Self` to another current role without explicit policy; or treating legacy `OwnDriverDataOnly` as the target scope.
17. Reactivating `Transferred` or `Terminated` DriverAssignments through generic editing.
18. Determining the current DriverAssignment from status alone, or treating a future-dated destination assignment as current before the transfer `EffectiveAt`.
19. Multiple User accounts linked to the same Employee.
20. Terminating a Driver Employee without closing the current DriverAssignment in the same operation.
21. Generating a per-assignment driver code, or creating a new Driver identity for a transfer.
22. Treating `DriverID` as a branch-tenure key, or branch payroll history as keyed by anything other than `DriverAssignmentID`.
23. A transfer-time rate-copy choice, or any transfer that creates, moves or copies rate or pay-rule records.
24. Recording a future-dated transfer as `Completed` before its `EffectiveAt`, or treating a temporary loan as a DriverAssignment.

---

## 17. Preserved Architecture

The refoundation must preserve the good existing architecture:

- `EmployeeID` as durable workforce identity;
- DriverAssignment history;
- multiple historical DriverAssignments per Driver where required;
- Driver transfer lineage;
- DriverAssignment effective windows;
- `DriverAssignmentID`-based payroll provenance and Driver-owned compensation;
- branch-bound composite foreign keys;
- payroll/rate history protections;
- optional `User.EmployeeID` relationship, with 0..1 ↔ 0..1 cardinality;
- Access Roles, Scope, and permission concepts;
- dedicated Driver Transfer workflow;
- future `DRIVER` self-service role;
- Payroll Setup architecture as already closed.

---

## 18. Non-Goals

This refoundation is not permission to build a general HR platform.

The Employee domain is limited to workforce identity, branch ownership, basic employment state, Driver specialization, future Import, and Access linking.

Out of scope unless separately approved:

- recruiting;
- benefits;
- attendance/time-clock redesign;
- performance management;
- HR document management;
- generalized organization charts;
- a full employment-lifecycle model, including the Driver → non-Driver transition (§4.6) and rehire/return (§4.7);
- speculative external integrations;
- full Driver portal implementation;
- designing specific Driver self-service commands;
- full C2 Import implementation;
- compensation architecture redesign;
- Payroll Setup redesign.

The goal is the smallest correct Workforce + Access foundation required for Flussra.

---

## 19. Acceptance Rules for the Later Refoundation

The later implementation is not considered complete merely because the People page looks better.

At minimum the completed refoundation must prove:

1. A non-Driver Employee can be created without a User.
2. A Driver can be created without a User.
3. A User can exist without an Employee where allowed.
4. A User can be explicitly linked to an existing same-company Employee, and link cardinality is 0..1 ↔ 0..1.
5. Role assignment does not create, move, or link workforce records.
6. `DRIVER` ⇔ `Self` for the current product; Self has no Access BranchID; a current Driver action requires a linked same-company Employee and that Employee's Driver's effective DriverAssignment (§7.4–§7.6).
7. Driver transfer preserves EmployeeID, the Driver (driver number and CDL number) and DriverAssignment history; it creates a new assignment, never a new Driver.
8. A future-dated transfer is `Scheduled`, not `Completed`, and does not make the destination assignment or Workforce branch current before its `EffectiveAt`; it can be cancelled or rescheduled only before then; Access Self authority is not synchronized to a branch.
9. Driver transfer cannot be reversed by later Role/Scope saving, and no DriverAssignment's branch can be updated.
10. Historical payroll remains on historical DriverAssignmentIDs; compensation follows the Driver with no transfer copy choice.
11. Generic People/Driver reads do not leak branch-roster data to Driver self-service users.
12. A DRIVER User cannot create, edit, approve, or void rates, payroll, or Workforce records, including their own; permission overrides cannot widen their resource scope or capability ceiling.
13. Workforce and Access creation have the defined transaction boundaries and retry behaviour (§11), and no login-enabled User exists without Role/Scope authority.
14. A pending DriverAssignment does not activate current-assignment DRIVER self-service before it becomes effective. A valid provisioned Self-only User can be represented by login and `/auth/me` without a fabricated branch-access row.
15. Terminating a Driver Employee closes the Employee and the current DriverAssignment in one operation without changing history; Self authorization fails closed when no effective DriverAssignment exists.
16. A permission override may add an action only within the User's established resource scope and may not bypass Self ownership or the DRIVER ceiling.
17. The protected `COMPANY_OWNER` dynamic-permission path requires a valid active company-wide owner assignment; malformed branch or Self scope is not company-wide authority.
18. Workforce import can later be built without requiring User/login creation.
19. Existing Payroll Setup authority remains untouched unless a concrete dependency is proven.

---

## 20. Source-of-Truth Priority

During implementation, conflicts are resolved in this order:

1. database financial and historical payroll invariants;
2. this locked architecture contract;
3. later product changes, but only after this contract has been explicitly amended to record them;
4. current backend behaviour where it does not conflict with the above;
5. current frontend behaviour;
6. legacy/dormant import design.

Current behaviour is evidence of what exists, not automatic authority for what should survive.

---

## 21. Next Step

With this contract locked, the next artifact is:

`PEOPLE_AND_ACCESS_REFOUNDATION_MASTER_PLAN.md`

That master plan must translate this contract into small implementation phases with:

- exact scope;
- migrations where required;
- backend changes;
- frontend changes;
- security hardening;
- test requirements;
- acceptance gates;
- independent review;
- clean development-data reset/reseed near the end.

C2 Import remains blocked until the People / Workforce / Access refoundation is closed.
