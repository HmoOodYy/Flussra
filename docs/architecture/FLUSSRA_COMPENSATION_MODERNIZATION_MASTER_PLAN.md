# Flussra Compensation Architecture Modernization — Master Implementation Plan

**Status:** APPROVED FOR IMPLEMENTATION — implementation intentionally deferred
**Document role:** Authoritative planning handoff between architecture/forensics and implementation execution
**Repository:** `HmoOodYy/Flussra`
**Plan freeze baseline:** `main @ cd1a0596d6ccfb165c588899e423a989959f80d5`
**Previous architecture-acceptance baseline:** `53e61b7bbe010f4f7d6f8fe687c25d2e3c951312`
**Implementation state:** NOT STARTED

> This document does **not** authorize immediate coding. It freezes the agreed target architecture and implementation dependency plan so work can resume later without repeating the investigation.

---

## 0. Resume Protocol — Read This First When Work Restarts

Because implementation is intentionally postponed, the first step when work resumes is **not coding**.

The implementation lead must first perform a short read-only drift review:

1. Fetch current `main` and record the new SHA.
2. Compare changes since `cd1a0596d6ccfb165c588899e423a989959f80d5`.
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

The latest merge at plan freeze (`cd1a0596`) adds Payroll Setup onboarding/readiness behavior. It is adjacent to this modernization and does not change the compensation target architecture.

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

Examples:

- Hours Worked
- Miles Driven
- Loads
- Wait Time
- Pallets
- Silos
- Overnight occurrences
- custom company definitions such as Stops

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

OrdinalTier is a mandatory first-production feature after PerUnit is proven.

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

## 2.8 CDPI

CDPI remains intentional.

Its role is configurable PayDefinition creation/governance.

It must stop manufacturing legacy RateTypes.

Method adapters evolve toward:

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

Example:

```text
VAC = 8 hours
rate = 25/hour
result = 200
```

`StatusRateColumn` remains **branch-owned**.

## 2.10 Company currency

First production:

```text
ONE COMPANY = ONE CURRENCY
```

Currency is explicit company configuration.

No per-rate multi-currency.

Historical evidence snapshots the CurrencyCode.

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
branch applicability via BranchPayItemConfig
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

# 4. System Definition Strategy

Current global system PayItems must not remain operational company-independent calculation authority.

The seven starter concepts are:

```text
HOURS
MILES
LOADS
WAIT_TIME
PALLETS
SILOS
OVERNIGHT
```

Target strategy:

1. Immutable **database starter catalog**.
2. Provision company-owned operational PayDefinitions from it.
3. Starter catalog is provisioning metadata only, never live compensation authority.

This permits:

```text
Company A:
LOADS -> PerUnit

Company B:
LOADS -> OrdinalTier
```

without abusing CDPI.

`BONUS`, `ADJUSTMENT`, `GUARANTEED_MINIMUM`, `SYS_MIN_TOPUP`, and `SYS_MAX_CAP` are not starter operational PayDefinitions.

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

For CDPI, approval/direct creation sets the target RateDefinition lock immediately according to CDPI's existing governance rule.

`CdpiDefinitions.LockedAtUtc` may temporarily remain as provenance only and must not remain a second edit-guard authority.

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

A temporary `ProvisioningState` may exist only to hide P4/P5A company definitions from the old runtime.

It is not product lifecycle state.

It must:

- never appear in API/UI contracts;
- survive only through the provisioning transition;
- be dropped at the end of P5B after the company-only operational cutover is complete.

---

# 6. Target Entity Graphs

## A. PerUnit HOURS

```text
Company A
    CurrencyCode = USD

Starter HOURS
    -> Company A PayDefinition HOURS
        CalculationMethod = PerUnit
        -> RateDefinition (Scalar)
            -> RateComponentDefinition: per_unit_rate

John
    -> DriverRateAssignment
        -> DriverRateValue = 25

8 hours -> 8 * 25 = 200 USD
```

## B. Custom PerUnit Stops

```text
CDPI request/direct creation
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

## C. OrdinalTier LOADS

```text
Company PayDefinition LOADS
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

# 10. CDPI Target Contract

CDPI remains a governance/creation workflow.

It must support:

- name;
- input type including WholeNumber;
- unit;
- PerUnit or OrdinalTier;
- method-specific structure;
- company ownership;
- branch availability;
- governance/audit history.

CDPI stops creating:

- `CDPI_*` RateTypes;
- PayItemRateTypeMap bridges;
- old RateSlot/RateType triples.

`CdpiDefinitions` remains during implementation as governance/provenance storage.

It may only be retired in a separately reviewed cleanup after every durable semantic has an explicit target home, including:

- origin;
- creation mode;
- schema version;
- creator;
- approval/creation provenance;
- lock provenance;
- request linkage where present;
- direct-create provenance.

`Origin='Custom'` alone is insufficient.

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
               P4 Starter provisioning
                  non-runtime
                    |
                    v
               P5A Target PerUnit authoring
                  non-operational
                    |
                    v
               P5B PayDefinition/CDPI/PerUnit
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
               P9 Rebaseline eligibility review
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
- Origin;
- StarterKey;
- temporary ProvisioningState.

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
- CDPI approval/direct create according to governance.

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

## P4 — Provision company-owned starter definitions, non-runtime

Create immutable DB starter catalog.

Create one idempotent database provisioning operation.

Provision the seven company-owned starter PayDefinitions.

No new RateType or PayItemRateTypeMap bridge is created.

Provisioned definitions remain hidden from current runtime via temporary ProvisioningState.

They must not reach:

- settings operational lists;
- period snapshots;
- day grid;
- current rate matrix;
- draft validation;
- live calculation.

**Authority:** legacy model remains sole operational authority.

---

## P5A — Target PerUnit rate authoring, still non-operational

Create scalar RateDefinition + `per_unit_rate` component for company starter definitions.

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
- target authoring exists only for hidden/provisioned definitions and is not live payroll authority.

---

## P5B — Operational PayDefinition/CDPI/PerUnit cutover

This is the first major authority transition.

### Company PayDefinitions

- ensure target RateDefinition exists for required custom CDPI PerUnit definitions;
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

Add WholeNumber to CDPI.

WholeNumber/Integer definitions reject fractional quantities for any calculation method.

### CDPI

PerUnit approval/direct create now creates target PayDefinition/RateDefinition directly.

No generated RateType/map/slot bridge.

`CdpiDefinitions` remains provenance only.

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

### CDPI

Add proposed tier topology storage.

Implement OrdinalTier adapter.

Remove unsupported RangeBracket/RangeProgressive/Block from active product method contract.

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
- old advanced-method calculators;
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

`CdpiDefinitions` is explicitly excluded from automatic deletion.

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

P9 does **not** perform the rebaseline.

It determines whether rebaseline is allowed.

Eligibility requires:

1. target domain model frozen;
2. fresh empty DB upgrades to target schema;
3. starter provisioning works;
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
| Baseline–P3 | Legacy/global + CDPI legacy runtime | Legacy RateType/DriverRate | Legacy | Legacy + projection | legacy dormant capability only | Legacy evidence; snapshot-only finalization |
| P4 | Legacy operational; company starters hidden | Legacy | Legacy | Legacy | not offered | Legacy |
| P5A | Legacy operational; company starters hidden | Legacy operational; target dormant authoring | Legacy | Legacy | not offered | Legacy |
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
| RangeBracket | REMOVE from product/runtime |
| RangeProgressive | REMOVE from product/runtime |
| Block | REMOVE from product/runtime |
| Fixed / Calculated / None / EnteredAmount as generic PayDefinition methods | REMOVE |
| ADJUSTMENT PayItem | REMOVE |
| generic Period Pay writes | REMOVE |
| PeriodPayMatrix report | KEEP / re-key target identities |
| BONUS PayItem authority | REMOVE; BonusEvent remains |
| GUARANTEED_MINIMUM authority | REMOVE; DriverPayRule remains |
| SYS_MIN_TOPUP / SYS_MAX_CAP PayItem rows | REMOVE only after explicit rule-output evidence replacement |
| STATUS_PAYMENT DraftLine projection | REMOVE after direct status path |
| CdpiDefinitions | KEEP during modernization; separate provenance-retirement decision later |
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
- CDPI governance;
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
- company A Loads PerUnit + company B Loads OrdinalTier simultaneously;
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
- starter catalog is provisioning-only;
- RateDefinition is the semantic compensation requirement;
- exactly one component abstraction exists;
- DriverRateAssignment is the effective-dated unit;
- DriverRateValue children have no independent dates;
- no authoritative overlaps;
- company/branch integrity enforced;
- canonical structure lock exists only on RateDefinition.

## Methods

- only PerUnit and OrdinalTier are current PayDefinition methods;
- unsupported legacy methods absent from current product/runtime.

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
- CDPI governed definitions lock according to CDPI approval rule.

## Company-specific behavior

- Company A Loads may be PerUnit;
- Company B Loads may be OrdinalTier;
- no leakage between companies;
- no CDPI workaround needed for starter method customization.

## CDPI

- PerUnit and OrdinalTier create target structures;
- no generated legacy RateTypes;
- custom definitions use same runtime as starters;
- provenance preserved.

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

- company currency explicit;
- no new durable monetary writes without currency;
- monetary rendering not hardcoded to `$`;
- finalized evidence snapshots CurrencyCode;
- configured currency cannot change after durable monetary state exists.

## Legacy elimination

- no operational RateType;
- no PayItemRateTypeMap;
- no legacy DriverRate authority;
- no generated CPI_/CDPI_/SRC_/STATUS_PAY identities;
- no unsupported advanced methods in runtime;
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
- one company currency;
- RateType is not target architecture;
- finalized history must not depend on live configuration.

---

# 20. Implementation Must Remain Deferred Until Explicitly Started

This plan exists so the current work can be completed first.

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

> Company-owned PayDefinitions and branch-owned StatusRateColumns own semantic RateDefinitions. Driver compensation resolves through one complete effective-dated DriverRateAssignment. PerUnit and OrdinalTier are the only first-production PayDefinition methods. Status, Bonus, and Pay Rules remain separate domains. Finalized payroll is explained by immutable target evidence. No operational workflow requires RateType, PayItemRateTypeMap, legacy DriverRate routing, generated compatibility rate identities, or unsupported legacy calculation methods.

That is the destination this plan authorizes.
