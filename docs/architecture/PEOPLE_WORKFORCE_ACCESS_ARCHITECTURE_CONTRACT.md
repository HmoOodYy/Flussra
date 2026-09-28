# PEOPLE / WORKFORCE / ACCESS ARCHITECTURE CONTRACT

**Status:** LOCKED — approved architecture boundary for the People / Workforce / Access refoundation  
**Scope:** Flussra People, Employee, Driver, User, Role, Scope, Driver Transfer, and future Import boundaries  
**Precondition:** Payroll Setup is closed and remains out of scope unless a concrete dependency is later proven  
**Implementation details:** belong to the subsequent master plan, not to this contract  
**Changes:** changing a locked architecture decision requires an explicit amendment to this contract  
**Next artifact after this contract:** `PEOPLE_AND_ACCESS_REFOUNDATION_MASTER_PLAN.md`

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

Implementation sequencing, migrations, endpoint design, UI component design, and test phasing belong in the later refoundation master plan.

The contract owns semantics and invariants. Where it states that an invariant must hold, the master plan chooses the mechanism (database constraint, trigger, service rule, scheduled activation, or effective-date resolution) unless this contract explicitly requires database enforcement.

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
│       └── Driver Profile(s)
│           ├── branch-specific
│           ├── effective-dated
│           ├── historical lineage
│           ├── Driver Pay Rates
│           └── Payroll history
│
└── Access
    │
    └── User (application login account)
        ├── optional Employee link (0..1 ↔ 0..1)
        ├── Role + Scope assignment
        └── Permission overrides
```

Workforce and Access are related domains, but neither is allowed to silently create or mutate the other. The only defined cross-domain effect is the alignment of the DRIVER self-service branch projection when a Driver transfer becomes effective (§7.6, §8.5).

### 2.1 Effective-date vocabulary

These terms are used throughout this contract:

- A Driver profile is **effective on date D** when D falls within its effective window (`EffectiveFrom` through `EffectiveTo`, inclusive; a NULL bound is open).
- The **current Driver profile** of an Employee is the profile effective on the current business date.
- A **historical Driver profile** is a profile whose effective window has ended.
- A **pending Driver profile** is a profile whose `EffectiveFrom` is in the future; it is not current.

Throughout this contract, "current" always means current and effective in this sense.

"Current" is never determined by status alone. Status and effective window must agree (§4.6), but the effective window decides which profile applies on a date.

---

## 3. Employee Contract

### 3.1 Canonical workforce identity

`core.Employees` is the canonical workforce-person domain.

`EmployeeID` is the durable workforce identity.

An Employee represents the person who works for the company, not their login account and not one particular Driver incarnation.

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

Whether an Employee is currently a Driver is determined only by the existence of a current Driver profile.

`EmployeeType`, if retained, is workforce metadata only:

- it never grants Access;
- it never determines authorization;
- it is not the authority for whether an Employee is a Driver and must not contradict Driver-profile truth;
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

For an Employee with **no** current or pending Driver profile, `Employee.BranchID` is the canonical current employment/home branch and is editable through the Employee workflow.

For an Employee **with** a current or pending Driver profile:

- the current Driver profile (or, if none is yet current, the pending one) is authoritative for the Driver branch;
- `Employee.BranchID` is a synchronized workforce projection of that profile's branch;
- it is not independently editable;
- it changes only as a consequence of Driver-profile creation or of a Driver transfer becoming effective (§8.3).

A first Driver profile for an existing Employee is created in the Employee's current `Employee.BranchID`. Any later branch change for a Driver is a Driver transfer.

---

## 4. Driver Contract

### 4.1 Driver is a profile, not a second person

`core.Drivers` represents a Driver-specific profile under an Employee.

A Driver record is not the canonical human identity.

The relationship is:

```text
Employee
   └── one or more Driver Profiles over time
```

### 4.2 DriverID semantics

`DriverID` is the branch/effective payroll identity.

It is intentionally used by Driver Pay Rates and payroll history.

Existing Driver history must not be collapsed into Employee history.

### 4.3 Branch and effective-date semantics

A Driver profile is:

- company-bound;
- branch-bound;
- effective-dated;
- historically preserved.

A historical Driver profile must remain available for payroll provenance.

Driver profile invariants:

1. A Driver profile and its Employee always belong to the same company.
2. For one Employee, Driver-profile effective windows must not overlap.
3. On any date, an Employee has at most one effective Driver profile.

### 4.4 Driver branch immutability

`Drivers.BranchID` is immutable after the profile is created.

There is no history-dependent exception: a profile's branch is never changed, whether or not payroll, rate, eligibility, entry, or snapshot records already reference it.

Working in a different branch means a Driver transfer, which creates a new Driver profile.

Role assignment, User editing, access-scope editing, Employee editing, or general People UI actions must never move a Driver profile between branches.

Driver-branch immutability, Driver/Employee same-company ownership, and at most one effective Driver profile per Employee per date are integrity invariants. They must be database-enforced where the database can express them; any part the database cannot express must be enforced in the owning service with tests.

### 4.5 Historical profiles

A historical Driver profile is never mutated into another branch or made current again.

Historical lineage and effective windows are authoritative and must remain intact.

### 4.6 Minimum Driver / Employee status rules

This contract defines only the status semantics required to protect payroll and transfer history. It is not an HR lifecycle.

1. A `Transferred` profile is closed by a Driver transfer. It remains effective through its `EffectiveTo` for payroll purposes and can never become current again after that. `Transferred` is set only by the Driver transfer operation.
2. A `Terminated` profile is never reactivated in place (§4.7). A later rehire/return, if ever supported, uses a separately defined lifecycle rather than reopening historical state.
3. Generic Driver or Employee editing must never reactivate a historical Driver profile or alter its effective window.
4. Statuses and effective windows must not contradict payroll eligibility: a profile that has ended (transferred or terminated) always has a closed effective window, and status and window describe the same effective period.
5. Driver and employment status values are a small, closed set enforced by the platform, not free text.
6. For Driver Employees, Employee `EmploymentStatus`, `HireDate`, and `TerminationDate` are payroll-eligibility inputs. Changing them must never rewrite locked or finalized payroll evidence.
7. Ending a Driver profile while the Employee remains employed in a non-Driver capacity (for example "stops driving, becomes office staff") requires payroll-eligibility semantics that are not currently defined. That transition workflow is deferred and is not part of this refoundation.

### 4.7 Terminating a Driver Employee

When a currently employed Driver Employee is terminated from employment on date D, one Workforce business operation applies:

```text
Employee:                         EmploymentStatus = Terminated, TerminationDate = D
Current Driver profile:           DriverStatus     = Terminated, EffectiveTo     = D
```

Both changes succeed or fail together. The terminated Employee's Driver profiles never become current after D; the master plan defines how any pending profile or in-flight transfer is closed.

Historical integrity:

- prior Driver profiles remain unchanged;
- Driver Pay Rates remain on their original `DriverID`s;
- payroll history, finalized evidence, snapshots, and used-rate evidence remain unchanged;
- termination never rewrites historical payroll.

A `Terminated` profile is never reactivated in place.

Termination is a Workforce operation and does not itself modify Access records. A DRIVER assignment whose linked Employee has no current Driver profile confers no current-profile self-service authorization (§7.6).

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

Link cardinality and same-company ownership are integrity invariants; the master plan chooses the exact constraints.

Access-role assignment must not be the mechanism that creates this link implicitly.

### 5.5 Provisioned and staged accounts

A **login-enabled** User must be in a valid provisioned state: it holds the Role/Scope authority required to use Flussra.

A User may exist without an active Role/Scope assignment only as an explicitly **staged/unprovisioned** account:

- login is disabled;
- the staged state is explicitly represented, not inferred from a missing assignment;
- it cannot authenticate or gain access by any path until it is provisioned.

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
- create a Driver profile;
- move an Employee to another branch;
- move a Driver profile to another branch;
- change EmployeeType;
- terminate or reactivate workforce records;
- create, change, or remove a User↔Employee link.

Removing or replacing the `DRIVER` role removes its `OwnDriverDataOnly` assignment and leaves the User↔Employee link and all workforce records unchanged.

The current DRIVER-role workforce side effects are explicitly retired.

### 6.3 Role and Scope remain Access concerns

Role, Scope, and permission overrides remain valid Access concepts.

Role and Scope form a single assignment and are never saved as separate operations.

Their implementation may be preserved where it is already correct, but their authority must stop at the Access boundary.

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
DRIVER  ⇔  OwnDriverDataOnly
```

- `OwnDriverDataOnly` scope is valid only with the `DRIVER` role.
- `DRIVER` is valid only with `OwnDriverDataOnly` scope.
- `DRIVER + SpecificBranch` and `DRIVER + AllCompanyBranches` are not valid configurations.
- A DRIVER self-service account must not receive general branch-wide access merely because the Employee belongs to that branch.
- Driver-role identity is matched by exact role identity/code `DRIVER`, never by substring or name matching.

### 7.4 DRIVER assignment preconditions

Assigning `DRIVER` requires that, at assignment time:

1. the User is already explicitly linked to an Employee (§5.4);
2. that Employee belongs to the User's company;
3. that Employee has a current Driver profile;
4. the `OwnDriverDataOnly` branch projection (§7.6) equals that current profile's branch.

If any precondition is not satisfied, the assignment is rejected.

Assigning `DRIVER` never creates, links, or modifies Employee or Driver records.

**Future Drivers.** A future Driver may be prepared before their Driver profile becomes effective. Before the profile's `EffectiveFrom` it is valid to have:

- the Employee;
- a pending Driver profile;
- an optional User account;
- an explicit User↔Employee link.

The pending profile is not current, so DRIVER self-service must not become active and the account must not gain DRIVER self-service authorization before that date. Access provisioned ahead of time remains an explicitly staged, login-disabled account (§5.5) until the Driver profile becomes effective. A pending profile is never treated as current merely so an account can be provisioned.

### 7.5 Self-service authorization model

Three concepts are distinct:

**Own identity.** Which Driver records belong to a DRIVER User is always derived from:

```text
User.EmployeeID → Employee → that Employee's Driver profiles
```

It is never derived from an Access-scope `BranchID`.

**Historical reads.** "Own data" may include the linked Employee's historical Driver profiles where a self-service feature permits history (for example historical pay or work records).

**Current actions.** A self-service action that operates on a Driver profile targets the linked Employee's profile effective on the date that operation's contract defines (normally the current business date).

A stale Access scope is never used to decide which Driver is "own".

### 7.6 ODA branch projection

The current permission system stores a `BranchID` on the `OwnDriverDataOnly` assignment.

Until the authorization architecture is deliberately changed:

- that `BranchID` is a synchronized authorization projection;
- it is not the authority for Driver identity;
- it must remain consistent with the linked Employee's current Driver profile.

If the linked Employee has no current Driver profile (for example after termination), the DRIVER assignment confers no current-profile self-service authorization; authorization fails closed.

When the current Driver profile changes branch because a transfer becomes effective, the ODA branch projection must become consistent with the new current profile as part of that transfer / effective-transition operation.

Unrelated administrative Access scopes never follow a workforce transfer (§8.6).

### 7.7 DRIVER capability boundary

The `DRIVER` role's effective permissions — including permissions granted to the role and any per-user permission overrides — must be limited to self-service-safe capabilities.

A DRIVER User must never obtain general administrative or mutation authority over:

- payroll;
- Driver Pay Rates;
- workforce / Employee / Driver administration;
- roles and permissions;
- other Drivers' data.

A DRIVER User must not be able to create, edit, approve, or void rates, payroll, or workforce records merely because the target belongs to themselves.

Future self-service commands (for example explicit request or acknowledgement workflows) are permitted only when separately designed as self-service operations. This contract does not design them.

Generic roster permissions such as `drivers.view` must not be the grant used to authorize a Driver's own self-service data.

### 7.8 Roster privacy

A Driver self-service User must not be able to browse the branch workforce roster or other Drivers' personal/operational information through generic People/Driver endpoints.

The self-only boundary must apply consistently before Driver self-service is exposed as a product feature.

---

## 8. Driver Transfer Contract

### 8.1 Durable Employee identity

A transfer preserves `EmployeeID`.

The person does not become a new Employee because they changed branches.

### 8.2 Driver profile transition

A Driver transfer must:

1. end the source Driver profile's effective window on the day before the transfer `EffectiveDate`;
2. preserve the source profile's historical identity and lineage;
3. create the destination Driver profile in the target branch;
4. start the destination profile's effective window on the transfer `EffectiveDate`;
5. preserve old payroll and rate references on the source `DriverID`.

Conceptually:

```text
Employee 153
│
├── Driver 500 — Source Branch — effective until EffectiveDate − 1, then historical
└── Driver 721 — Target Branch — effective from EffectiveDate
```

### 8.3 Effective-dated transition

A transfer must never make the destination profile or destination branch current before the transfer `EffectiveDate`, regardless of when the transfer was approved or administratively completed.

Example — approved October 1, `EffectiveDate` November 1:

- through October 31 the source profile remains the current Driver profile, and the source branch remains the Employee's current workforce branch;
- from November 1 the destination profile is current, `Employee.BranchID` reflects the destination branch, and DRIVER self-service authorization follows the destination profile.

Every consequence of "current" — the `Employee.BranchID` projection (§3.5), the ODA branch projection (§7.6), self-service current actions (§7.5), and current workforce views — follows the effective date, not the approval or completion timestamp.

The mechanism (delayed completion, effective-date resolution, scheduled activation, or another safe approach) is chosen by the master plan.

### 8.4 Driver Pay Rates on transfer

The product does not automatically decide whether a transferred Driver keeps the same rates. Driver transfer supports an explicit choice, conceptually:

```text
Transfer / copy Driver Rates to the destination profile?
```

Historical integrity (always):

- existing source Driver rates never move and are never rebound to the destination `DriverID`;
- they remain attached to the source Driver profile for historical payroll and rate provenance.

Choice = No:

- the destination Driver profile receives no rates from the source merely because of the transfer.

Choice = Yes:

- equivalent applicable rates are created for the destination Driver profile;
- they are new rate records owned by the destination `DriverID`;
- source rate records remain unchanged.

```text
Source Driver 100
  rate history remains on Driver 100

Transfer with rates = YES
        ↓
Destination Driver 200
  new equivalent rate records created for Driver 200
```

This is copy semantics, never move semantics. There is no silent automatic carry-forward.

The master plan defines the UI wording, which rows are selectable, destination effective dates, overlap/conflict resolution, and whether a preview is shown. It must preserve source historical rate evidence, destination `DriverID` ownership, the explicit user choice, and the absence of silent carry-forward. Copied rates are ordinary destination rate records subject to the existing rate integrity rules.

### 8.5 Linked DRIVER self-service User

When the transferred Employee has a linked User whose Access Role is `DRIVER`, the transfer keeps that self-service access aligned with the Employee's current Driver profile:

- own identity continues to be derived from the link (§7.5) and therefore needs no rewrite;
- the ODA branch projection (§7.6) becomes consistent with the destination profile when the transfer becomes effective (§8.3).

This is part of the defined transfer business operation, not a later role re-save.

### 8.6 Administrative roles remain independent

A workforce transfer must not automatically move unrelated administrative access.

For example, a Payroll Manager or Company-level administrative User may have access scope independent of the Employee's current employment branch.

For every non-DRIVER role, an Employee's branch change has no automatic effect on that User's Access.

Only access defined as Driver self-service follows the Driver transfer.

---

## 9. Pay Rates and Payroll History Contract

### 9.1 Preserve DriverID authority

Driver Pay Rates and historical payroll remain keyed to `DriverID` according to the existing payroll model.

This is intentional.

### 9.2 Never rewrite history because an Employee moved

A transfer must not rewrite old payroll, rates, finalized evidence, snapshots, or eligibility history to the destination DriverID.

Carrying rates forward is only ever the explicit copy defined in §8.4.

### 9.3 Import resolution rule

A future importer must resolve operational Driver identity using enough context to identify exactly one effective Driver profile.

The expected conceptual resolution is:

```text
Company
+ EmployeeKey (or a C2-defined external identifier that maps to exactly one Employee)
+ Branch
+ Work/Effective Date
        ↓
the Employee's Driver profile in that branch effective on that date
        ↓
DriverID
```

"Effective" has the meaning defined in §2.1.

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
Create Driver Employee = Create Employee + Create initial Driver Profile
```

```text
Add first Driver Profile to an existing Employee
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

Generic workforce read endpoints must not turn `OwnDriverDataOnly` — or the `DRIVER` role's default permissions — into branch-wide workforce visibility.

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
└── Driver Profile when applicable
```

It must not create User login accounts by default.

### 14.2 Access Import

If bulk Access provisioning is ever required, it is a separate explicit workflow.

It must not be an implicit side effect of workforce import.

### 14.3 Driver Rate Import

Rate import must resolve the correct effective Driver profile (§9.3) before writing a Driver rate.

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
5. DRIVER role assignment creating a Driver profile.
6. DRIVER role assignment implicitly linking a User to an Employee.
7. Role/Scope editing moving Driver or Employee branch.
8. Any direct update of an existing Driver profile's branch.
9. EmployeeKey being derived from UserID.
10. A role save being capable of undoing a completed Driver transfer.
11. Driver-section visibility depending only on Access Role rather than workforce truth.
12. Requiring a password/login account merely to add a Driver to the workforce.
13. Treating `/core/people` as sufficient Employee management when it is read-only.
14. Allowing Driver self-service users general branch-roster visibility.
15. Detecting the Driver role by substring matching of role codes.
16. `OwnDriverDataOnly` scope assigned with a non-DRIVER role, or `DRIVER` assigned with a non-ODA scope.
17. Reactivating `Transferred` or `Terminated` Driver profiles through generic editing.
18. Determining the current Driver profile from status alone, or treating a future-dated destination profile as current before the transfer `EffectiveDate`.
19. Multiple User accounts linked to the same Employee.
20. Terminating a Driver Employee without closing the current Driver profile in the same operation.

---

## 17. Preserved Architecture

The refoundation must preserve the good existing architecture:

- `EmployeeID` as durable workforce identity;
- Driver profile history;
- multiple historical Driver profiles per Employee where required;
- Driver transfer lineage;
- Driver effective windows;
- `DriverID`-based rate and payroll provenance;
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
6. `DRIVER` ⇔ `OwnDriverDataOnly`, and `DRIVER` assignment is rejected unless its preconditions (§7.4) hold.
7. Driver transfer preserves EmployeeID and Driver history.
8. A future-dated transfer does not make the destination profile or branch current before its `EffectiveDate`.
9. Driver transfer cannot be reversed by later Role/Scope saving, and no Driver profile's branch can be updated.
10. Historical rates/payroll remain on historical DriverIDs; destination rates exist only through the explicit copy choice.
11. Generic People/Driver reads do not leak branch-roster data to Driver self-service users.
12. A DRIVER User cannot create, edit, approve, or void rates, payroll, or workforce records, including their own.
13. Workforce and Access creation have the defined transaction boundaries and retry behaviour (§11), and no login-enabled User exists without Role/Scope authority.
14. A pending Driver profile does not activate DRIVER self-service before it becomes effective.
15. Terminating a Driver Employee closes the Employee and the current Driver profile in one operation without changing history.
16. Workforce import can later be built without requiring User/login creation.
17. Existing Payroll Setup authority remains untouched unless a concrete dependency is proven.

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
