# Flussra Compensation Architecture Modernization — Master Implementation Plan

**Status:** APPROVED TARGET ARCHITECTURE — P3b is the current implementation work unit
**Document role:** Authoritative Compensation target architecture and implementation dependency plan
**Repository:** `HmoOodYy/Flussra`
**Current baseline:** Post-G0 accepted `main @ f2937bfdcbe1266a00716905a8712f3fe29baff2` (PR #45 merge); Alembic head `0081`. Historical earlier baseline: `main @ f671a2ee61e9d31b0804af95eaff9973978a94af`, with P2c PR #28 merged as `af00a22915f93810d123aadea18c3a73da1360aa`
**Execution state:** P2c, Phase 2, P3a (PR #30) and G0 (G0.1–G0.6, PR #34 through PR #45) are closed; P3b — Target Compensation schema/invariants — is the current implementation work unit and advances the migration head from `0081` to `0082`

> This document records the approved target architecture. P3b is the current implementation unit; P3c and later units have not started.

> **Naming note:** The Unified Refoundation Execution Plan controls work-unit boundaries and order. The internal phase labels in §12–§13 below (for example "P3A Core target schema" and "P3B Evidence-v2 schema foundation") are not Unified work-unit identifiers. The core target schema described under §13 "P3A" is delivered by the Unified P3b work unit; evidence structures are sequenced by the Unified plan's later units.

---

## 0. Resume Protocol — Read This First When Work Restarts

P3b is the current implementation unit. Before each later work unit starts, the implementation lead performs a read-only drift review:

1. Fetch current `main` and record the new SHA.
2. Compare changes since the current approved baseline recorded above.
3. Confirm that no newly merged work has changed:
   - PayDefinition / PayItem authority;
   - RateType / DriverRate authority;
   - CDPI;
   - StatusRateColumn / status pay;
   - calculation snapshot/evidence;
   - finalization;
   - BonusEvent / DriverPayRule;
   - company currency assumptions;
   - branch/company provisioning.
4. If nothing materially changes this plan, proceed from the approved phases below.
5. If material drift exists, update **only the affected phase/dependency**, not the entire architecture from scratch.
6. Do not re-open frozen product decisions unless a real technical impossibility or new explicit product requirement exists.

P2c merged through PR #28 as `af00a22915f93810d123aadea18c3a73da1360aa`; Test Hygiene PR #29 merged afterward. P3a closed via PR #30 and G0 closed with PR #45. The post-G0 accepted baseline is `main @ f2937bfdcbe1266a00716905a8712f3fe29baff2` at migration head `0081`; P3b advances it to `0082`.

---

# 1. Executive Target

## Current problem

Flussra currently contains two generations of compensation architecture at once.

The older runtime path is approximately:

```text
PayItem
    -> PayItemRateSlot
    -> PayItemRateTypeMap
    -> RateType
    -> DriverRate
```

`RateType` began largely as a technical routing identity but accumulated business responsibilities. `DriverRate` also accumulated method-specific behavior such as tiers, blocks, and rounding.

That structure no longer matches the product.

## Target

```text
Company
    -> company-owned PayDefinition
        -> CalculationMethod
        -> RateDefinition
            -> RateComponentDefinition(s)
                -> DriverRateAssignment
                    -> DriverRateValue(s)

Branch-owned StatusRateColumn
    -> RateDefinition
        -> DriverRateAssignment
            -> DriverRateValue(s)
```

Core rule:

```text
CalculationMethod defines the required compensation shape.
Definition structure owns that shape.
DriverRateAssignment supplies one complete effective-dated value set.
```

The effective-dated parent assignment is atomic. Child values never have independent effective dates.

## First-production calculation methods

Exactly:

```text
PerUnit
OrdinalTier
```

No first-production requirement exists for:

```text
RangeBracket
RangeProgressive
Block
Fixed
Calculated
None
EnteredAmount
```

## Separate domains remain separate

```text
PTO / Sick / Off / Holiday -> Status domain
Bonus                     -> BonusEvent
Minimum / Maximum Pay     -> DriverPayRule
Manual Adjustment         -> not a product feature
```

---

# 2. Frozen Product Decisions

These are product decisions, not implementation suggestions.

## 2.1 Pre-production freedom

There is no valuable production payroll data to preserve.

Therefore obsolete architecture may be removed and local databases may be rebuilt.

However, future production historical integrity is mandatory.

## 2.2 PayDefinition boundary

A PayDefinition represents measured work/business input that directly produces compensation.

Examples only (not reserved identities or required definitions): `ITEM_ALPHA`, `ITEM_BETA`, Custom Units, Stops, Trips, Hours Worked, Miles Driven, Loads, Wait Time, Pallets, Silos, and Overnight occurrences. No code or display name has privileged runtime semantics.

Not PayDefinitions:

- statuses;
- Bonus;
- minimum/maximum rules;
- generic manual money adjustments.

## 2.3 PerUnit

```text
amount = quantity * driver-specific scalar rate
```

Example:

```text
8 hours * 25 = 200
```

Its method requirement is a scalar compensation requirement such as:

```text
per_unit_rate
```

## 2.4 OrdinalTier

OrdinalTier is a first-production method alongside PerUnit. These are the only methods in the initial production scope; the target method boundary remains extensible for separately approved future product methods.

Frozen semantics:

```text
quantity = 4

ordinal 1  -> 3
ordinal 2  -> 4
ordinal 3+ -> 5

result = 3 + 4 + 5 + 5 = 17
```

Rules:

- quantity is integer/discrete;
- positions begin at 1;
- no gaps;
- no overlaps;
- ordering deterministic;
- exactly the final tier is open-ended;
- final tier must be open-ended;
- negative values forbidden;
- zero is valid;
- missing/unconfigured is NULL/unset and is **not** zero;
- Approved assignment requires every required value.

## 2.5 Method owns shape; assignment fills values

For OrdinalTier:

```text
Definition structure:
    ordinal 1
    ordinal 2
    ordinal 3+

John's assignment values:
    3
    4
    5
```

John does not redefine the ordinal topology.

## 2.6 Effective dating

One complete compensation schedule is one `DriverRateAssignment` version.

Child values do not carry their own effective dates.

The resolver must never combine values from different assignment versions.

## 2.7 RateType

Generic RateType is not a target domain concept.

Target removals/replacements include:

- generic `RateTypes`;
- `PayItemRateTypeMap`;
- generated `CPI_*` RateTypes;
- generated `CDPI_*` RateTypes;
- generated `SRC_*` / `STATUS_PAY` RateTypes;
- `DriverRates.RateTypeID`;
- `/payroll/rate-types` operational catalog.

Every current consumer must have a target replacement before deletion.

## 2.8 CDPI transition

CDPI is transitional legacy product terminology and workflow architecture, not a permanent target compensation identity. Its useful request, approval/direct-create, creator/approver, audit, method/input proposal, branch applicability, and provenance semantics move into generic PayDefinition governance. The target has one PayDefinition model; the calculator does not branch on whether a definition originated in CDPI.

During transition, existing CDPI paths stop manufacturing legacy RateTypes and move to generic PayDefinition governance without dual-writing one request into two authorities. `CdpiDefinitions` or other legacy CDPI storage may remain temporarily only until equivalent generic provenance is implemented and verified.

Method-owned requirements include:

```text
PerUnit
    -> ScalarRateRequirement

OrdinalTier
    -> OrdinalScheduleRequirement
```

## 2.9 Status pay

Status remains separate from PayDefinition.

Target:

```text
DayEntryState
    -> StatusKey
        HoursValue
        StatusRateColumn
            -> scalar RateDefinition
                -> DriverRateAssignment
                    -> DriverRateValue

amount = HoursValue * rate
```

This is the current first-production Status compensation policy, not a universal formula for every future paid status. Status remains a separate domain. Any future fixed amount or other status compensation policy requires explicit product architecture.

Example:

```text
VAC = 8 hours
rate = 25/hour
result = 200
```

`StatusRateColumn` remains **branch-owned**.

## 2.10 Company currency

Current first-production/V1 product boundary:

```text
ONE COMPANY = ONE CURRENCY
```

Currency is explicit company configuration. No per-rate multi-currency is supported in V1. This is a product boundary, not a claim that multi-currency is impossible forever; any future support requires an explicit model for rate/payment currencies, FX source/date, rounding, reconciliation, and historical evidence. Historical evidence snapshots the CurrencyCode.

## 2.11 Structural immutability

Once the compensation structure becomes authoritative/used, it does not mutate casually.

Changing:

```text
1 / 2 / 3+
```

to:

```text
1 / 2+
```

changes the compensation contract, not merely a driver's rate.

Full definition-versioning is deliberately deferred until a real future requirement exists.

## 2.12 Domain invariants, V1 boundaries, and hard-coding limits

### Permanent domain invariants

The target preserves finalized-payroll immutability; Employee identity remains distinct from User identity; Status, BonusEvent, and DriverPayRule remain separate from PayDefinition; missing values are not zero; one effective DriverRateAssignment supplies one complete value set; historical evidence is not reconstructed from mutable current configuration; and finalization reads approved immutable evidence rather than live compensation state.

### Current-product / V1 boundaries

The following are strict current-product boundaries, not universal claims about every future product: exact `DRIVER` is the only currently supported binding to generic `Self`; one Company has one configured CurrencyCode; PerUnit and OrdinalTier are the first-production PayDefinition methods; Branch is the current operational applicability dimension; the approved Status compensation path is the V1 policy; DriverID remains the branch-bound historical payroll/compensation identity; and active DRIVER/Self authority retains the current generic capability ceiling, including denial of mixed DRIVER/administrative authority.

`Self` is a generic resource scope, not branch membership, and has no BranchID. `DRIVER ⇔ Self` is the current product binding only. A future role may use Self only after its ownership and capability policy is explicitly designed. The current DRIVER denial remains authoritative unless a separate product/security decision changes it. Before final P7 Access UX is frozen, product/security design must revisit whether one person needs both Driver self-service and administrative responsibilities; separate accounts, explicit persona/session context, or another model remain undecided.

Branch remains the current applicability dimension. Do not add Region, Department, Team, Project, Union, or a generic scope engine now. Future applicability expansion must be designed without cloning one configuration table per dimension.

Company currency is one-per-company for V1. Future multi-currency requires explicit rate currency, payment currency, FX source/date, rounding, reconciliation, and historical-evidence rules.

PerUnit and OrdinalTier are the only first-production methods. Legacy RangeBracket, RangeProgressive, Block, Fixed, Calculated, and EnteredAmount implementations may be removed from current runtime; that does not permanently prohibit a future product capability with similar business meaning. Future methods require explicit product approval and plug into method-owned structure, validation, calculation, and evidence boundaries.

Status remains separate from PayDefinition. The current V1 status compensation policy uses `HoursValue × scalar rate`; this is not a universal formula for every future paid status. A fixed daily amount, percentage policy, or another StatusKeyPayRule would require separate product architecture and is not implemented here.

DriverID remains intentional for current driver/fleet payroll. Generic payroll for arbitrary non-driver employees would require a separate architecture evolution; this plan does not introduce an abstract CompensationSubject. PayDefinition remains an approved compensation-producing operational/business input, not a generic bucket for Status, BonusEvent, DriverPayRule, finalized corrections, or unrelated adjustment/event domains. Other earning concepts—including recurring allowances, salary, commissions, per diem, shift premiums, or other earnings—require explicit future product/domain classification; the target does not claim every possible earning is quantity multiplied by a rate.

### Forbidden accidental hardcoding

No business PayDefinition code or display name may have privileged runtime semantics. Code, database, API, and UI behavior must not branch on names such as `HOURS`, `MILES`, or `LOADS`; ordinary role display names or substrings; presentation labels; generated technical identities; or CDPI origin. Resource ownership follows canonical IDs and relations, never labels or names. `Self` is never treated as a Branch. Protected system role codes such as `COMPANY_OWNER` and current-product `DRIVER` may have explicit, centrally defined semantics; ordinary custom role names do not drive authorization, display names never do, and adding a business role must not require scattered role-code conditionals. Stable versioned domain enums such as `PerUnit`, `OrdinalTier`, `AllCompanyBranches`, `SpecificBranch`, and `Self` remain valid. Extend behavior through explicit domain-owned policy/method boundaries, not name conditionals; this is a strong domain model, not a generic rules/plugin/formula engine.

---

# 3. Target Domain Model

## Company

Responsibilities:

```text
Company
    CurrencyCode
```

All company monetary values inherit this currency.

## PayDefinition

Operational PayDefinitions are **company-owned**.

Conceptual fields:

```text
identity
CompanyID
code
name
input type
unit
CalculationMethod
status
origin/provenance
generic governance and approval provenance
branch applicability through the current Branch configuration authority (currently `BranchPayItemConfig`)
```

The physical table may continue to be named `PayItems` during implementation, but its target meaning must be PayDefinition.

## CalculationMethod

First-production registry:

```text
PerUnit
OrdinalTier
```

Responsibilities:

- define input requirements;
- define compensation shape;
- validate method structure;
- validate assignment completeness;
- execute arithmetic;
- expose algorithm/schema version for evidence when needed.

## RateDefinition

One complete semantic compensation requirement owned by a legitimate domain owner.

First-production owners:

```text
PayDefinition
StatusRateColumn
```

It is not a generic routing catalog.

It may represent:

```text
Scalar
OrdinalTierSchedule
```

## RateComponentDefinition

One structural component model replaces the useful semantics currently split across slots/mappings.

Scalar example:

```text
per_unit_rate
```

Ordinal example:

```text
component 1: ordinal 1..1
component 2: ordinal 2..2
component 3: ordinal 3..infinity
```

## DriverRateAssignment

Atomic effective-dated parent:

```text
AssignmentID
CompanyID
BranchID (scope/integrity, not independent compensation dimension)
DriverID
RateDefinitionID
EffectiveFrom
EffectiveTo
Status
approval/audit metadata
```

Logical compensation identity:

```text
DriverID + RateDefinitionID
```

`BranchID` may remain denormalized/enforced for permissions, filtering, and composite integrity because Driver already belongs to one branch incarnation.

## DriverRateValue

```text
AssignmentID
RateComponentDefinitionID
Amount
```

Rules:

- no independent effective date;
- no independent lifecycle status;
- `Amount >= 0`;
- zero valid;
- NULL means missing;
- exactly one required value per component before approval.

---

# 4. PayDefinition Creation and Optional Templates

No business PayDefinition code or display name has privileged runtime semantics. A company is valid with zero PayDefinitions, and company creation/provisioning must not require a named business definition. No mandatory starter catalog, seed row, immutable system definition, special database identity, onboarding definition, schema dependency, migration assumption, runtime branch, API/UI behavior, or acceptance gate may be based on `HOURS`, `MILES`, `LOADS`, `WAIT_TIME`, `PALLETS`, `SILOS`, `OVERNIGHT`, or another business name/code.

If a future product-approved convenience offers suggested definitions such as Hours, Miles, Loads, or Stops, those are optional template content only:

```text
optional template -> clone/create -> ordinary company-owned PayDefinition
```

After creation, the result has no privileged identity. Templates are not core architecture, required seed data, runtime/schema authority, special IDs/codes, fresh-database or company-validity requirements, or part of the P3/P4 core acceptance gate. This plan does not design a template subsystem.

If payroll readiness later needs configured definitions for a particular workflow, its requirement must be generic and based on the workflow's configured capabilities. Do not invent a readiness requirement here or test for a named definition.

Human examples may use arbitrary business names/codes such as `ITEM_ALPHA`, `ITEM_BETA`, Custom Units, Stops, or Trips. Tests must demonstrate that assignment and calculation behavior is independent of predefined business names.

---

# 5. Approved Special Invariants

## 5.1 Pending assignments and structure edits

Structure change is fail-closed while any Pending DriverRateAssignment exists for the RateDefinition.

Blocked changes include:

- PayDefinition CalculationMethod;
- structural DataType;
- RateDefinition Shape;
- inserting/updating/deleting components or tiers.

Pending assignments must be explicitly **discarded** first.

### Pending lifecycle

- Pending is editable in place.
- At most one Pending assignment per logical identity.
- Pending never transitions to Voided.
- Explicit discard physically deletes the Pending assignment and its values.
- Discard writes the full discarded value set to audit history.
- Nothing authoritative may reference a Pending assignment.

`Voided` is reserved for previously authoritative Approved/Superseded assignments.

Concurrency must prevent structure changes racing with creation/approval of Pending assignments.

## 5.2 One canonical structure lock

There is exactly one enforcement authority:

```text
RateDefinitions.StructureLockedAtUtc
```

Do **not** create an authoritative PayDefinition lock column in parallel.

The lock is set by whichever occurs first:

- first Approved DriverRateAssignment;
- first payroll-period reference/snapshot containing the RateDefinition;
- later authoritative evidence reference if somehow not already set.

Generic PayDefinition approval/direct-create governance sets the target RateDefinition lock according to its approved lifecycle. Legacy CDPI lock data may temporarily remain as provenance only; it must not become a second target edit-guard authority.

Status-owned RateDefinitions use the same lock model.

## 5.3 Currency gate begins immediately

Once the currency phase lands, a company without configured CurrencyCode may read existing history but may not create new durable monetary state.

At minimum currency is required before:

- creating/updating monetary BonusEvents;
- creating/reactivating/changing monetary DriverPayRules;
- creating or approving legacy DriverRates while they still exist;
- legacy Period Pay writes while they still exist;
- submitting/resubmitting/finalizing payroll;
- target DriverRateAssignment approval once target assignments exist;
- any other durable monetary writer discovered in the phase audit.

Changing one configured currency to another becomes blocked after durable company monetary state exists.

At minimum durable state includes:

- approved/superseded rate assignments;
- legacy approved/superseded DriverRates while present;
- BonusEvents;
- active/ended DriverPayRules;
- calculation snapshots/finalized payroll.

## 5.4 ProvisioningState is temporary scaffolding

A temporary `ProvisioningState` may exist only if needed to keep generic target definitions non-operational before authority cutover. It is not named-definition provisioning metadata and is not a required target lifecycle field.

It is not product lifecycle state.

It must:

- never appear in API/UI contracts;
- survive only through the defined authority transition;
- be dropped at the end of P5B after the company-only operational cutover is complete.

---

# 6. Target Entity Graphs

## A. PerUnit arbitrary definition

```text
Company A
    CurrencyCode = USD

Company-owned PayDefinition ITEM_ALPHA
    CalculationMethod = PerUnit
        -> RateDefinition (Scalar)
            -> RateComponentDefinition: per_unit_rate

John
    -> DriverRateAssignment
        -> DriverRateValue = 25

8 hours -> 8 * 25 = 200 USD
```

## B. Generic-governed PerUnit definition

```text
PayDefinitionRequest or approved direct creation
    Name = Stops
    InputType = WholeNumber
    Unit = Stop
    Method = PerUnit
        -> Company PayDefinition
            -> scalar RateDefinition
                -> per_unit_rate

John value = 7.50
5 stops -> 37.50 company-currency units
```

## C. OrdinalTier arbitrary definition

```text
Company PayDefinition ITEM_BETA
    CalculationMethod = OrdinalTier
        -> RateDefinition
            component 1: ordinal 1
            component 2: ordinal 2
            component 3: ordinal 3+

John Assignment V1
    component 1 = 3
    component 2 = 4
    component 3 = 5

quantity 4 -> 3 + 4 + 5 + 5 = 17
```

## D. Paid VAC status

```text
DayEntryState
    -> StatusKey VAC
        HoursValue = 8
        -> branch-owned StatusRateColumn
            -> scalar RateDefinition
                -> John's DriverRateAssignment
                    value = 25

8 * 25 = 200
```

## E. Bonus

```text
BonusEvent
    Driver
    Amount
    company currency
    business/audit metadata
```

No PayDefinition or RateDefinition.

## F. Minimum pay

```text
DriverPayRule MinimumPay
    -> policy calculation
    -> explicit rule-output evidence/line kind
```

The rule is not a PayDefinition.

---

# 7. PerUnit Contract

A PerUnit definition owns exactly one scalar component:

```text
per_unit_rate
```

Rate validation:

```text
Amount < 0   -> invalid
Amount = 0   -> valid configured value
Amount NULL  -> missing/unconfigured
```

Approval requires a non-NULL value.

Calculation:

```text
amount = quantity * resolved assignment value
```

WholeNumber input definitions reject fractional quantities even under PerUnit.

Historical evidence preserves:

- PayDefinition identity and snapshots;
- quantity;
- unit;
- CalculationMethod;
- algorithm version;
- RateDefinition identity;
- DriverRateAssignment identity;
- resolved value;
- CurrencyCode;
- result.

---

# 8. OrdinalTier Contract

## Definition structure

Example:

```text
1..1
2..2
3..infinity
```

Validation:

- first tier starts at 1;
- all bounds are positive integers;
- deterministic ordering;
- no gaps;
- no overlaps;
- exactly one open-ended tier;
- open-ended tier is final;
- WholeNumber input required.

## Assignment values

The definition owns:

```text
1
2
3+
```

The driver assignment owns:

```text
3
4
5
```

## Required arithmetic

```text
quantity = 4
1 = 3
2 = 4
3+ = 5

result = 3 + 4 + 5 + 5 = 17
```

## Atomicity

Resolve exactly one DriverRateAssignment first.

Then load all values by that AssignmentID.

Never resolve the latest value independently per component.

---

# 9. Status-Pay Contract

Target direct path:

```text
canonical DayEntryState
    -> StatusKey
    -> HoursValue
    -> StatusRateColumn
    -> RateDefinition
    -> DriverRateAssignment
    -> DriverRateValue
    -> status-pay calculation
    -> snapshot/final evidence
```

Current generated RateTypes disappear.

`STATUS_PAYMENT` DraftLine remains temporarily as a non-authoritative compatibility projection until all downstream consumers move.

It is then removed.

Cross-branch copying:

- default status column may map default -> default;
- custom columns are **not** matched by normalized name;
- unmatched custom columns are skipped and reported unless a future explicit stable mapping product is approved.

---

# 10. Generic PayDefinition Governance and CDPI Transition

The target governance concepts are generic `PayDefinitionRequest`, `PayDefinitionApproval` / approval provenance, `PayDefinition`, definition provenance, and branch applicability. Request creation, approval, product-approved direct creation, creator/approver identity, request linkage, timestamps, method/input/unit proposal, audit/governance history, schema/version provenance, creation-mode provenance, structure-lock provenance, and historically required branch context must remain explainable.

The final calculator, runtime, domain, API, and UI use one PayDefinition model and do not distinguish a “CDPI PayDefinition” from a normal PayDefinition. CDPI-specific identity is transitional legacy terminology, not a target identity.

During transition, generic governance must preserve the currently approved behavior for:

- proposed name and input type, including WholeNumber;
- unit;
- approved calculation method and method-specific structure;
- company ownership;
- branch applicability;
- governance/audit history.

The legacy CDPI runtime stops creating:

- `CDPI_*` RateTypes;
- PayItemRateTypeMap bridges;
- old RateSlot/RateType triples.

`CdpiDefinitions` and related legacy CDPI storage may remain temporarily as transition/provenance storage only until equivalent generic PayDefinition governance/provenance is present and verified. Before retirement, the target must provide an explicit home for origin, request linkage, creator, approver, creation mode, approval/direct-create provenance, schema/method version, lock provenance, timestamps/audit meaning, and branch applicability/context where historically required. `Origin='Custom'` alone is insufficient. Do not guess historical backfills. Once equivalence is proven, retire CDPI-specific runtime terminology, APIs, UI, service identity, and obsolete schema in the appropriate cleanup phase; preserve historical migrations.

---

# 11. Transition Strategy

Use **short-lived additive target + authority cutover + prompt legacy deletion**.

Do not:

- maintain long-lived dual write;
- backfill disposable legacy local rate data merely for compatibility;
- create new PayItemRateTypeMap bridges for company-owned definitions;
- keep two authorities for the same compensation domain.

Temporary target structures may exist dormant before cutover, but only one model is authoritative per domain at any time.

---

# 12. Approved Phase Dependency Graph

```text
P0
Freeze decisions and baseline
        |
        +------------------------+
        v                        v
P1 Currency                P2 narrow dead-writer cleanup
        |                        |
        +-----------+------------+
                    v
               P3A Core target schema
                    |
                    +------> P3B Evidence-v2 schema
                    |
                    v
               P4 Generic definition governance/provenance readiness
                  non-runtime
                    |
                    v
               P5A Target PerUnit authoring
                  non-operational
                    |
                    v
               P5B Generic PayDefinition/PerUnit
                  operational cutover
                    |
                    v
               P5C Evidence/hash/finalization
                  read-model cutover
                    |
             +------+------+
             |             |
             v             v
          P6A Status      P7 OrdinalTier
             |
             v
          P6B Remove STATUS_PAYMENT projection
             |             |
             +------+------+
                    v
               P8A Operational legacy removal
                    v
               P8B Legacy evidence/DriverRates cleanup
                    v
               P8C FK inventory + predecessor cleanup
                    + delete legacy PayItems
                    + drop RateTypes
                    v
               P8D Docs/tests/final smoke
                    v
               P9 Rebaseline eligibility review (standalone Compensation-plan review; not a new refoundation macro phase)
```

P6 and P7 may execute in either order once P5C is complete.

---

# 13. Phase Details

## P0 — Freeze decisions and record implementation-time baseline

When implementation actually begins:

- perform Resume Protocol;
- record current SHA;
- baseline test counts;
- verify fresh-database migration;
- reconfirm no architectural drift.

**Authority:** legacy production model only.

---

## P1 — Company currency

Add company CurrencyCode.

Rules:

- initially unconfigured allowed for existing companies;
- first explicit configuration allowed;
- invalid code rejected;
- durable monetary writes require configured currency immediately;
- changing currency after durable monetary state exists is blocked;
- history reads remain allowed.

Replace hardcoded monetary `$` / `USD` rendering with shared company-currency-aware formatting.

Audit and gate all current durable monetary writers.

**Authority:** legacy compensation still authoritative; currency rule becomes target production invariant immediately.

---

## P2 — Retire proven-dead legacy writers

Remove only surfaces with no desired current product use, after zero-consumer verification.

Candidates:

- generic Period Pay write/list API and service;
- legacy custom-item create/update superseded by CDPI;
- predecessor CustomPayItemRequests write paths where proven obsolete;
- obsolete rate-assignment helpers;
- dead frontend wizard branch.

Retire but do not prematurely physically delete dependent PayItem rows.

Keep:

- `PeriodPayMatrix` read-only report;
- `/reports/period-pay` read path;
- BonusEvent;
- current SYS_* rule-output compatibility until P5C replacement exists.

**Authority:** legacy compensation only.

---

## P3A — Core target compensation schema

Add target schema alongside legacy model.

### PayDefinition target fields

Conceptually:

- CalculationMethod;
- company ownership and generic origin/provenance;
- governance state needed by the approved lifecycle;
- branch applicability through ordinary company definitions.

No business-specific StarterKey or starter provisioning state is required. Any transitional field must have a named cutover purpose and removal point.

Do **not** add authoritative PayItem structure-lock state.

### RateDefinitions

- exactly one legitimate owner:
  - PayDefinition; or
  - StatusRateColumn;
- company integrity;
- Shape;
- semantic key;
- `StructureLockedAtUtc` as canonical structure lock.

### RateComponentDefinitions

- component identity;
- ordering;
- scalar/ordinal structure;
- whole-schedule validation for OrdinalTier.

### DriverRateAssignments

- identity = DriverID + RateDefinitionID;
- CompanyID/BranchID enforced scope;
- effective interval;
- lifecycle;
- overlap exclusion;
- at most one Pending per identity;
- at most one current Approved per relevant identity according to lifecycle policy.

### DriverRateValues

- amount >= 0;
- unique assignment/component;
- component must belong to assignment RateDefinition.

### Pending discard

- Pending values editable only while Pending;
- structure change blocked while Pending exists;
- explicit discard deletes Pending + values and writes audit history;
- Pending cannot become Voided;
- concurrent structure edit vs Pending creation/approval cannot both commit.

### Structure lock

Set canonical RateDefinition lock on:

- first Approved assignment;
- first authoritative payroll period reference;
- generic PayDefinition approval/direct-create according to the approved governance lifecycle.

**Authority:** legacy runtime only; target tables dormant.

---

## P3B — Evidence-v2 schema foundation

Add target evidence structures but no writer yet.

Conceptual evidence contains:

- source kind;
- PayDefinition/Status/Rule identity as appropriate;
- CalculationMethod;
- algorithm version;
- RateDefinition;
- DriverRateAssignment;
- component topology/values;
- effective dates;
- CurrencyCode;
- immutable labels/snapshots.

Avoid naming collision with existing legacy `...UsedRateDefinitions`; use distinct target evidence terminology such as `UsedCompensation`.

**Authority:** legacy evidence only.

---

## P4 — Generic PayDefinition governance/provenance readiness, non-operational

Establish the generic company-owned PayDefinition creation/governance and provenance path needed before operational cutover. This phase exists to complete the accepted generic lifecycle and durable provenance seam, not to provision named starter definitions. Standalone P4 does not create an additional Unified macro or work unit. Its non-operational governance and provenance obligations are satisfied through the corresponding dependency-closed Unified work units, primarily P3b/P3c before the P4a authority switch. This mapping does not authorize combining, skipping, or reordering Unified §6A work units.

The governance path preserves approved request creation, approval, product-approved direct creation, creator/approver identity, request linkage, method/input/unit proposal, creation mode, schema/method version, branch applicability, timestamps/audit meaning, and structure-lock provenance. Legacy CDPI workflow data may remain a temporary transition source until each required durable meaning has an explicit target home and migration/backfill behavior is proven without guessing. Do not dual-write one request into legacy and target authorities.

A company with zero PayDefinitions remains valid. Optional future templates may create ordinary company-owned definitions, but no template/catalog/activation is required by this phase or the P3/P4 core acceptance gate.

**Authority:** legacy compensation remains the sole operational authority until the P4a cutover described in the Unified Plan. Generic target governance may be tested while non-operational.

---

## P5A — Target PerUnit rate authoring, still non-operational

Create scalar RateDefinition + `per_unit_rate` component for eligible company-owned PayDefinitions, regardless of code or display name.

Implement target assignment lifecycle:

- create Pending;
- edit complete scalar value set;
- approve atomically;
- supersede previous assignment;
- history;
- summary;
- atomic copy;
- explicit discard for Pending;
- void rules for previously authoritative assignments;
- company/currency/permission checks.

Build target API/client/view-model without rewiring live payroll.

Approving a target assignment locks its RateDefinition structure.

**Authority:**

- legacy remains operational;
- target authoring may create arbitrary company-owned definitions but is not live payroll authority before cutover.

---

## P5B — Operational generic PayDefinition/PerUnit cutover

This is the first major authority transition.

### Company PayDefinitions

- ensure the target RateDefinition exists through generic PayDefinition governance for each operational definition requiring compensation;
- repoint BranchPayItemConfig to company definitions;
- make company definitions operational;
- remove global PayItems from operational payroll authority;
- all operational PayItem/PayDefinition readers become company-only.

No legacy rate-data conversion is required for disposable dev DBs.

### ProvisioningState removal

At the close of P5B, once:

- all operational readers are company-only;
- BranchPayItemConfig is repointed;
- global PayItems are outside runtime authority;

**drop ProvisioningState**.

It must never become long-term state.

### Input types

Ensure generic PayDefinition governance supports WholeNumber input; transition the legacy request field without preserving a separate CDPI identity.

WholeNumber/Integer definitions reject fractional quantities for any calculation method.

### Legacy CDPI transition

The former CDPI request/approval/direct-create behavior now uses generic PayDefinition governance and creates the ordinary target PayDefinition/RateDefinition structure. No separate CDPI target identity or generated RateType/map/slot bridge remains. Required durable provenance has an explicit generic target home; legacy CDPI storage remains only until equivalent provenance is verified.

### Pay Rates

PayDefinition rate authoring uses target assignments.

Status may still use legacy rate plumbing until P6A.

### Live PerUnit calculation

Target flow:

```text
Company PayDefinition
    -> RateDefinition
    -> resolve one DriverRateAssignment by WorkDate
    -> DriverRateValue
    -> PerUnit calculator
```

Missing != zero.

### Interim evidence

Until P5C, target PerUnit lines must carry complete immutable evidence in the already-hashed immutable line evidence JSON/source snapshot path so no finalized target line depends on live assignments.

A temporary gap in the structured "rates used" view is acceptable only if full immutable line evidence exists and P5C follows promptly.

**Authority:**

- company PayDefinitions + target assignments = PayDefinition authority;
- legacy DriverRate path may remain only for Status until P6A;
- no two rate models are authoritative for the same source domain.

---

## P5C — Structured evidence/hash/finalization/read-model cutover

Introduce target evidence writer and new canonical hash/evidence version.

Replace legacy PayDefinition RateType/DriverRate identities with target identities.

Finalization must continue its strongest invariant:

```text
approved immutable calculation snapshot is sole finalization authority
```

Finalization must not re-resolve live assignments.

Add explicit rule-output evidence for minimum/maximum outputs so SYS_* PayItem rows can later disappear.

Move:

- finalized library;
- reports;
- ledger;
- rates-used views;

onto v2 evidence for PayDefinitions and rule outputs.

Status may still use legacy structured evidence until P6A.

**Authority:** target evidence for PayDefinitions/rules; legacy evidence only for status.

---

## P6A — Status pay target cutover

Branch-owned StatusRateColumns gain scalar hourly RateDefinitions.

Existing and newly created branches/columns receive target definitions.

Stop generating:

- `STATUS_PAY` RateType;
- `SRC_*` RateTypes.

Move matrix/history/batch/status resolver to target assignments.

Replace technical RateCode line-type leakage with fixed semantic status source vocabulary.

Cross-branch copy:

- default -> default;
- custom unmatched -> skip/report.

The stored `STATUS_PAYMENT` compatibility DraftLine may continue temporarily, but is explicitly non-authoritative.

**Authority:** target assignments everywhere for rates; status projection remains compatibility-only.

---

## P6B — Remove STATUS_PAYMENT compatibility projection

Remove physical status-payment stand-in lines from:

- day-grid sync;
- lifecycle refresh;
- source-line filters;
- projection SQL;
- unique indexes;
- tests expecting the compatibility row.

Target flow becomes directly canonical:

```text
DayEntryState
    -> status calculation
    -> review snapshot
    -> final evidence
```

---

## P7 — OrdinalTier

May run before/after P6 once P5C is complete, but must be after PerUnit architecture is proven.

### Definition editing

Unlocked PayDefinitions may change method/topology only if:

- RateDefinition is not locked;
- no Pending assignment exists.

Pending assignments must be explicitly discarded first.

### Generic definition governance

Use the same PayDefinition request/approval/direct-create governance for OrdinalTier structure; do not add a CDPI-specific adapter identity.

Implement OrdinalTier method structure. Remove the legacy RangeBracket/RangeProgressive/Block implementations from current runtime when their replacement gates pass. Similar future product capabilities are not permanently prohibited; they require explicit approval and the target method-owned boundary.

### Runtime

Ordinal values are one complete multi-value assignment.

Copy, where supported, copies the full assignment atomically.

Pure calculator semantics:

```text
for ordinal in 1..quantity:
    find definition component covering ordinal
    add that assignment component's value
```

Required test:

```text
1 = 3
2 = 4
3+ = 5
quantity = 4
result = 17
```

Evidence stores topology, values, expanded explanation, currency, assignment identity, algorithm version.

---

## P8A — Remove operational legacy compensation layer

After P5C + P6B + P7 are complete:

Remove operational code/APIs for legacy RateType/DriverRate compensation.

Expected cleanup includes:

- legacy rate endpoints;
- CDPI-specific active runtime terminology, APIs/UI/service identity, and obsolete adapter logic after generic governance/provenance equivalence is proven;
- any obsolete mandatory starter/provisioning scaffolding that remains after the generic authority transition;
- legacy implementations for methods outside the first-production PerUnit/OrdinalTier scope;
- PayItemRateTypeMap;
- old PayItemRateSlots;
- DriverRateTiers;
- Block fields;
- PayItems RateBehavior;
- PayrollPeriodPayItems RateBehavior;
- StatusRateColumns.RateTypeID;
- legacy generated code paths.

`DriverRates` and `RateTypes` may temporarily remain as dead schema only until FK/evidence cleanup is complete.

---

## P8B — Legacy evidence identities and DriverRates cleanup

Remove legacy evidence identities after v2 evidence is authoritative.

Examples:

- snapshot `RateTypeID`;
- snapshot `DriverRateID`;
- final-line `RateTypeID`;
- final-line `DriverRateID`;
- final-line legacy RateBehavior;
- legacy UsedRateDefinitions table;
- DriverTotals.PeriodPay once proven obsolete.

Then drop `DriverRates` after all its dependents are removed.

Do **not** yet assume global/SYS PayItem rows can be deleted.

---

## P8C — Full FK inventory, predecessor cleanup, legacy rows, RateTypes

Before destructive PayItem/RateType deletion, query the database FK catalog and enumerate every child constraint.

### PayItem dependents

Handle every remaining child by:

- repointing;
- clearing;
- dropping a proven-dead table/constraint;
- or proving zero legacy references.

Audit includes at minimum:

- PayItemSettings;
- PayItemLineTypeMap;
- BranchPayItemConfig;
- PayrollRunBonuses;
- period/final structures;
- custom/predecessor request/provenance structures;
- any additional FK discovered dynamically.

If immutable legacy local DB rows still reference obsolete IDs, migration aborts with a clear rebuild instruction rather than deleting historical rows to force migration.

### RateType dependents

Before RateTypes drop, remove every child, including:

- PayProfile-era tables;
- DriverRates;
- status RateType FK;
- map/slot/tier structures;
- legacy evidence identities;
- any additional FK found in the live catalog.

### Deletion order

1. clear/drop all remaining PayItem/RateType children;
2. delete obsolete global starter-era PayItem rows and fake/system output rows only after replacements are live;
3. drop RateTypes only when its FK inventory is empty.

`CdpiDefinitions` is excluded from automatic deletion until equivalent generic PayDefinition provenance is proven for all required durable meaning and its retirement receives explicit review.

---

## P8D — Final docs/tests cleanup and end-to-end smoke

Update superseded architecture docs.

For every deleted structural test, identify the replacement invariant test.

Final fresh-database smoke must cover:

- PerUnit;
- OrdinalTier;
- status pay;
- BonusEvent;
- minimum top-up/rule output;
- submit -> review -> finalize -> Finalized Library;
- two companies with different currencies.

Static audit confirms no operational RateType mental model remains.

---

## P9 — Migration rebaseline eligibility review

P9 is a standalone Compensation-plan eligibility review, not a new refoundation macro phase, and does **not** perform the rebaseline.

It determines whether rebaseline is allowed.

Eligibility requires:

1. target domain model frozen;
2. fresh empty DB upgrades to target schema;
3. generic PayDefinition creation/governance and required provenance work with arbitrary definitions; no starter content is required;
4. company currency works;
5. PerUnit end-to-end passes;
6. OrdinalTier end-to-end passes;
7. status pay uses target assignment model;
8. BonusEvent and DriverPayRule remain separate;
9. generic Adjustment/Period Pay writer removed;
10. no operational RateType dependency;
11. no PayItemRateTypeMap requirement;
12. old DriverRate/DriverRateTier runtime authority gone;
13. v2 evidence/finalization/finalized library complete;
14. backend/frontend/schema tests pass from fresh DB;
15. static audit finds no unintended live legacy references;
16. independent architecture review confirms this plan's acceptance criteria.

Only then may a separate approved migration-history consolidation begin.

---

# 14. End-of-Phase Authority Table

| End of phase | PayDefinition discovery/config | Rate authoring | Live PerUnit | Status pay | OrdinalTier | Evidence/finalization |
|---|---|---|---|---|---|---|
| Baseline–P3 | Legacy/global + transitional CDPI runtime | Legacy RateType/DriverRate | Legacy | Legacy + projection | legacy dormant capability only | Legacy evidence; snapshot-only finalization |
| P4 | Legacy operational; generic target governance/provenance is non-operational | Legacy | Legacy | Legacy | not offered | Legacy |
| P5A | Legacy operational; arbitrary company PayDefinitions available to target dormant authoring | Legacy operational; target dormant authoring | Legacy | Legacy | not offered | Legacy |
| P5B | **Company PayDefinitions** | **Target for PayDefinitions; legacy only for status** | **Target** | Legacy + projection | not offered | target PayDefinition line evidence; status legacy structured evidence |
| P5C | Company PayDefinitions | Target / legacy-status only | Target | Legacy + projection | not offered | **v2 for PayDefinitions/rules; legacy status only** |
| P6A | Company PayDefinitions | **Target everywhere** | Target | **Target** + non-authoritative projection | maybe P7 | v2 everywhere |
| P6B | Company PayDefinitions | Target | Target | Target, no projection | maybe P7 | v2 |
| P7 | Company PayDefinitions | Target scalar + schedules | Target | Target | **Target** | v2 including topology |
| P8+ | Target only | Target only | Target | Target | Target | v2 only |

There is no planned phase with two unintended authoritative models for the same compensation domain.

---

# 15. Legacy Deletion Register

| Component | Target decision |
|---|---|
| CPI architecture | REMOVE |
| old custom Daily creation | REMOVE |
| RateTypes | REMOVE after full consumer/FK cutover |
| PayItemRateTypeMap | REMOVE |
| current RateSlots | REPLACE with RateDefinition/components |
| generated CPI_* | REMOVE |
| generated CDPI_* | REMOVE |
| generated SRC_* / STATUS_PAY | REMOVE |
| old DriverRates | REPLACE |
| DriverRateTiers | REMOVE after Ordinal target |
| BlockSize / RoundingRule | REMOVE |
| Legacy RangeBracket implementation | REMOVE from current runtime after replacement; a similar future product capability is not permanently prohibited |
| Legacy RangeProgressive implementation | REMOVE from current runtime after replacement; a similar future product capability is not permanently prohibited |
| Legacy Block implementation | REMOVE from current runtime after replacement; a similar future product capability is not permanently prohibited |
| Legacy Fixed / Calculated / None / EnteredAmount PayDefinition implementations | REMOVE from current runtime; future capabilities require explicit product design |
| ADJUSTMENT PayItem | REMOVE |
| generic Period Pay writes | REMOVE |
| PeriodPayMatrix report | KEEP / re-key target identities |
| BONUS PayItem authority | REMOVE; BonusEvent remains |
| GUARANTEED_MINIMUM authority | REMOVE; DriverPayRule remains |
| SYS_MIN_TOPUP / SYS_MAX_CAP PayItem rows | REMOVE only after explicit rule-output evidence replacement |
| STATUS_PAYMENT DraftLine projection | REMOVE after direct status path |
| CdpiDefinitions and related CDPI storage | TRANSITIONAL provenance only; retire after equivalent generic PayDefinition provenance is explicitly present and verified |
| PayProfile-era tables | REMOVE only after zero-consumer proof |
| PayrollRunBonuses predecessor | REMOVE only after zero-consumer/FK proof |
| ProvisioningState | TEMPORARY; remove at end of P5B |

---

# 16. Historical / Finalization Contract

Current finalization's best invariant must survive:

```text
Finalization projects the approved immutable calculation snapshot.
It does not resolve current live rates again.
```

Target evidence must preserve enough information to explain payroll without current mutable configuration.

For PayDefinition earnings:

- PayDefinition ID/code/name snapshot;
- input quantity;
- unit;
- CalculationMethod;
- algorithm/schema version;
- RateDefinition ID/key;
- DriverRateAssignment ID;
- exact resolved values;
- CurrencyCode;
- result.

For OrdinalTier additionally:

- exact topology;
- exact tier values;
- enough detail to explain `3 + 4 + 5 + 5 = 17`.

For status:

- StatusKey/label;
- HoursValue used;
- StatusRateColumn;
- RateDefinition;
- DriverRateAssignment;
- resolved scalar rate;
- CurrencyCode;
- result.

For DriverPayRule outputs:

- Rule identity/type;
- threshold/value snapshot;
- inputs before rule;
- derived result;
- CurrencyCode.

---

# 17. Test Migration Principles

Keep business invariants; delete tests whose only purpose is preserving obsolete structure.

## Preserve/rewrite

- PerUnit arithmetic;
- effective WorkDate resolution;
- no authoritative overlap;
- approval/supersession;
- company isolation;
- branch applicability;
- generic PayDefinition governance and durable provenance, including the transition from legacy CDPI;
- exact OrdinalTier arithmetic;
- tier topology validation;
- atomic assignment resolution;
- status-pay arithmetic;
- canonical DayEntryState;
- BonusEvent;
- DriverPayRule;
- calculation snapshot immutability;
- finalization snapshot-only behavior;
- finalized evidence integrity.

## Delete after replacement coverage

- RateType identity tests;
- PayItemRateTypeMap mechanics;
- CPI cleanup/backfills;
- generated CDPI RateType triple tests;
- generated status RateType tests;
- RangeBracket/RangeProgressive/Block product tests;
- DriverRateTier persistence tests;
- obsolete migration-repair behavior after rebaseline.

## Mandatory new tests

- zero vs missing;
- negative rejection;
- final Ordinal tier open-ended;
- gaps/overlaps rejected;
- Pending blocks structure change;
- discard Pending permits structure change;
- concurrent Pending creation vs structure edit cannot both commit;
- one canonical structure lock;
- atomic tier assignment;
- company A `ITEM_ALPHA` PerUnit + company B `ITEM_BETA` OrdinalTier simultaneously, proving behavior does not depend on a predefined business name;
- WholeNumber PerUnit fractional rejection;
- currency write gates;
- company currency immutability;
- no RateType dependency after cutover;
- direct status path after projection removal;
- finalization never queries live assignment data.

---

# 18. Final Acceptance Criteria

The modernization is complete only when all applicable statements are objectively true.

## PayDefinition / Rate architecture

- every operational PayDefinition is company-owned;
- a company is valid with zero PayDefinitions; no named starter or starter catalog is required; optional templates, if later approved, create ordinary company-owned definitions;
- arbitrary definition names/codes do not affect calculation or assignment behavior;
- RateDefinition is the semantic compensation requirement;
- exactly one component abstraction exists;
- DriverRateAssignment is the effective-dated unit;
- DriverRateValue children have no independent dates;
- no authoritative overlaps;
- company/branch integrity enforced;
- canonical structure lock exists only on RateDefinition.

## Methods

- only PerUnit and OrdinalTier are current PayDefinition methods;
- legacy implementations outside the first-production method set are absent from current runtime; this does not prohibit separately approved future methods with similar semantics.

## PerUnit

- `8 * 25 = 200` end-to-end;
- zero valid;
- NULL missing;
- negative rejected;
- correct old WorkDate resolves old assignment.

## OrdinalTier

- integer input required;
- ordinal starts at 1;
- no gaps;
- no overlaps;
- final tier always open-ended;
- zero valid;
- missing blocks approval;
- topology owned by definition;
- values owned by one assignment;
- no mixed AssignmentIDs;
- `1=3, 2=4, 3+=5, quantity=4 -> 17`.

## Pending / structure

- Pending assignment blocks structural changes;
- explicit discard removes Pending + values and audits them;
- no automatic reinterpretation;
- first Approved assignment or first period reference locks definition;
- governed definitions lock according to the generic approved PayDefinition lifecycle.

## Company-specific behavior

- different companies may use arbitrary definitions with different methods;
- no leakage between companies;
- no name/code-specific path or starter customization mechanism is needed.

## Definition governance and legacy CDPI

- generic PayDefinition governance creates PerUnit and OrdinalTier structures;
- no generated legacy RateTypes;
- all ordinary definitions use the same runtime regardless of origin;
- required provenance is preserved generically before legacy CDPI storage retires.

## Status

- status remains separate from PayDefinition;
- StatusRateColumn remains branch-owned;
- status compensation uses target assignment;
- `VAC 8h * 25 = 200`;
- no generated status RateTypes;
- compatibility DraftLine removed after cutover.

## Other domains

- Bonus remains BonusEvent;
- min/max remains DriverPayRule;
- generic Adjustment gone;
- generic Period Pay writer gone;
- PeriodPayMatrix report remains.

## Currency

- company currency explicit for V1 (one currency per company); future multi-currency requires explicit architecture;
- no new durable monetary writes without currency;
- monetary rendering not hardcoded to `$`;
- finalized evidence snapshots CurrencyCode;
- configured currency cannot change after durable monetary state exists.

## Legacy elimination

- no operational RateType;
- no PayItemRateTypeMap;
- no legacy DriverRate authority;
- no generated CPI_/CDPI_/SRC_/STATUS_PAY identities;
- no legacy implementation outside the first-production method set remains in runtime;
- no temporary ProvisioningState after P5B.

## Historical integrity

- snapshots preserve target identities and resolved values;
- OrdinalTier evidence preserves topology and schedule;
- finalized payroll is independently explainable;
- finalization never re-resolves live compensation;
- mutable future configuration cannot alter finalized meaning.

## Validation

- focused target suites pass;
- DB integrity suites pass;
- full backend regression passes;
- frontend build/lint/tests pass;
- fresh DB upgrade passes;
- fresh DB end-to-end smoke passes;
- static legacy-dependency audit passes;
- independent architecture review passes before rebaseline.

---

# 19. Implementation-Lead Rules

The implementation lead may decide:

- final table/class names;
- module decomposition;
- exact API route names;
- exact DB techniques for exclusion/locking/immutability;
- transaction implementation;
- permission reuse where consistent;
- read-model composition;
- whether CdpiDefinitions can eventually retire after equivalent provenance exists.

The implementation lead may **not** reinterpret:

- PerUnit semantics;
- OrdinalTier semantics;
- `3 + 4 + 5 + 5 = 17` requirement;
- method owns shape / assignment fills values;
- no independent tier effective dates;
- zero != missing;
- final tier open-ended;
- PayDefinitions company-owned;
- StatusRateColumns branch-owned;
- Driver assignment identity centered on DriverID + RateDefinitionID;
- Status outside PayDefinition;
- BonusEvent outside PayDefinition;
- DriverPayRule outside PayDefinition;
- no generic Adjustment feature;
- one company currency as a V1 product boundary;
- RateType is not target architecture;
- finalized history must not depend on live configuration.

---

# 20. Implementation Must Remain Deferred Until Explicitly Started

This plan is a target and dependency reference. P2c, Phase 2, P3a and G0 are closed. P3b is the current implementation work unit; later phases remain subject to their stated dependencies and review gates.

No implementation should begin merely because this document exists.

When the user later says to start the modernization:

1. perform the Resume Protocol;
2. have the implementation lead revalidate dependency drift;
3. select only the first approved work unit;
4. implement/review/test that unit separately;
5. do not jump ahead to later cleanup phases;
6. require explicit review at every authority cutover.

---

# Final Architectural Test

If P1–P8 are implemented correctly, Flussra must end with exactly this property:

> Company-owned PayDefinitions and branch-owned StatusRateColumns own semantic RateDefinitions. Driver compensation resolves through one complete effective-dated DriverRateAssignment. PerUnit and OrdinalTier are the only first-production PayDefinition methods; arbitrary business codes/names carry no privileged meaning. Status, BonusEvent, and DriverPayRule remain separate domains. Finalized payroll is explained by immutable target evidence. No operational workflow requires RateType, PayItemRateTypeMap, legacy DriverRate routing, generated compatibility identities, or legacy implementations outside first-production scope. Future product capabilities remain subject to explicit domain design.

That is the destination this plan authorizes.
