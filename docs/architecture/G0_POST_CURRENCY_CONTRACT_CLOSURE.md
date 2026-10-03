\# FLUSSRA â€” G0 POST-CURRENCY CONTRACT CLOSURE

\## Architectural Context and Explanation Only

This document is an architectural handoff for the next Flussra work unit.

It is intentionally \*\*not an implementation prompt\*\*.

Do not treat this document as instructions to immediately modify code. Its purpose is to explain:

\- where Flussra currently stands;
\- what changed after P3a;
\- what architectural and correctness problems were exposed by the P3a work;
\- why a narrow closure stage is required before P3b;
\- what G0 contains;
\- why each G0 workstream exists;
\- what is intentionally outside G0;
\- and what must be true before target Compensation work begins.

The implementation will be performed separately after this architectural intent is understood.

\---

\# 1. Current Baseline

The current accepted baseline is:

```text
main
737c7b3549fcee9756e2f26fd3743a088b912150

Alembic head:
0076

P3a â€” Company Currency Authority
CLOSED
```

P3a introduced Company-owned currency authority and established a major new financial invariant:

```text
ONE COMPANY
\&#x20;   â†’
ONE CONFIGURED CURRENCY
\&#x20;   â†’
ALL NEW DURABLE MONETARY STATE
MUST BELONG TO THAT DENOMINATION
```

It also established frozen currency evidence in calculation snapshots and final payroll data.

P3a deliberately did \*\*not\*\* redesign Compensation.

Legacy compensation remains operational.

The major Compensation refoundation still begins with P3b.

However, a full reread of the current repository after P3a revealed several problems that should be resolved \*\*before\*\* creating the target Compensation schema.

Those findings are the reason G0 exists.

\---

\# 2. Why G0 Exists

G0 is not a new product feature.

It is not a replacement for P3b.

It is not another large refoundation phase.

G0 is a \*\*closure gate between P3a and P3b\*\*.

The purpose is simple:

> P3a changed a fundamental financial invariant and exposed several legacy assumptions, dead authorities, precision inconsistencies, and concurrency couplings that should not be carried into the new Compensation architecture.

The sequence should therefore be:

```text
P3a
Company Currency Authority
\&#x20;       âœ…

\&#x20;       â†“

G0
Post-Currency Contract Closure

\&#x20;       â†“

P3b
Target Compensation foundation
```

Without G0, P3b would begin while the repository still contains avoidable legacy financial writers and while some of the currency contract is internally inconsistent.

That would mean designing the new architecture while continuing to pay compatibility costs for things we already know should disappear.

That is unnecessary, especially because Flussra is still \*\*pre-production\*\*.

\---

\# 3. The Pre-Production Principle

This point is fundamental to every G0 decision.

Flussra is \*\*PRE-PRODUCTION\*\*.

There is no production customer database whose historical behavior must be preserved.

There is no production payroll history that justifies architectural compromises.

Development/demo data is disposable.

Therefore:

> Existing code, tables, routes, APIs, migrations, tests, and compatibility behavior are not valuable merely because they already exist.

Legacy must earn its continued existence by serving the target product.

We must not preserve a bad or obsolete architecture because:

\- it took effort to build;
\- tests currently reference it;
\- migrations created it;
\- replacing it requires cleanup;
\- or compatibility would otherwise be easier.

The priority remains:

```text
Correctness
\&#x20;   â†“
Architecture quality
\&#x20;   â†“
Security / integrity
\&#x20;   â†“
Maintainability
\&#x20;   â†“
Scalability
\&#x20;   â†“
Developer experience
\&#x20;   â†“
Compatibility with obsolete legacy behavior
```

If a cleaner solution requires:

\- deleting a dead route;
\- deleting unused tables;
\- changing tests;
\- dropping development data;
\- resetting/reseeding the database;
\- replacing an old compatibility layer;
\- or refusing to preserve an obsolete contract;

that is acceptable.

In pre-production, preserving the wrong architecture is more expensive than changing it now.

At the same time, this is not permission for indiscriminate deletion.

Every claimed-dead surface must first be proven to have no desired runtime consumer.

The principle is:

```text
Do not preserve legacy merely because it exists.

But also:

Do not delete something merely because it looks old.
Prove whether it still owns a real product responsibility.
```

\---

\# 4. Why G0 Comes Before P3b

P3b is expected to establish the clean target Compensation model.

That work should not inherit unnecessary dependencies from the old system.

Before creating new entities such as:

```text
PayDefinition
RateDefinition
RateComponentDefinition
DriverRateAssignment
DriverRateValue
```

we want the existing repository to have a much clearer financial boundary.

The current legacy system still contains multiple monetary or compensation-related concepts such as:

```text
PayItems
RateTypes
DriverRates
DriverRateTiers
PayItemRateTypeMap
PayItemRateSlots
PayProfiles
PayProfileRates
generic Period Pay
legacy custom-pay-item writers
CDPI
BonusEvent
DriverPayRule
status-payment projection
```

Not all of these are equally valuable.

Some still have real product meaning.

Some are transitional.

Some appear to be historical leftovers.

G0 separates those categories before P3b begins.

\---

\# 5. G0 Is Composed of Six Workstreams

G0 contains six primary workstreams.

They are:

```text
G0.1 â€” Monetary Precision Contract Closure

G0.2 â€” Continuous Validation / CI Baseline

G0.3 â€” Currency Concurrency and Lock Ownership Cleanup

G0.4 â€” Generic Period Pay Writer Retirement

G0.5 â€” Predecessor Custom Pay Item Writer Retirement

G0.6 â€” Pay Profile Family Dead-Authority Audit and Cleanup
```

These workstreams are related because they all remove ambiguity exposed by P3a.

They are not Compensation redesign itself.

P3b begins only after these boundaries are understood and closed.

\---

\# 6. G0.1 â€” Monetary Precision Contract Closure

\## Why this exists

P3a correctly added support for Company currencies with different ISO minor-unit precisions.

Examples include:

```text
JPY â†’ 0 decimal minor units

USD / EGP â†’ 2

KWD â†’ 3

CLF â†’ 4
```

However, one important legacy monetary domain still assumes exactly two decimal places:

```text
PayrollBonusEvents.Amount
```

The current Bonus storage uses approximately:

```text
NUMERIC(18,2)
```

and the Bonus batch validation explicitly rejects values containing more than two fractional digits.

The current Bonus UI also contains input behavior based on:

```text
step = 0.01
```

This means the system can declare KWD or CLF to be supported Company currencies while one canonical monetary domain cannot faithfully represent legitimate values in those currencies.

For example:

```text
1.234 KWD
```

is representable by the currency contract but not by the existing Bonus contract.

That is a correctness inconsistency.

\---

\## Important distinction

G0 must not solve this by declaring:

> every monetary value should always use the currency minor-unit precision.

That would also be wrong.

There are three different concepts:

```text
1\\. Rate / calculation precision

2\\. Currency amount / source precision

3\\. Final payable rounding
```

They are related but they are not the same.

A compensation rate may legitimately require more decimal precision than the smallest payable currency unit.

For example:

```text
0.1234 currency units per mile
```

may be a valid rate even if the currency normally settles to two decimals.

Flussra's current financial calculation core already largely uses:

```text
NUMERIC(18,4)
```

and 4-decimal internal precision.

That is useful and should not be casually reduced.

\---

\## What G0.1 is trying to achieve

G0.1 should make the current monetary domains internally consistent with the P3a currency contract.

The conceptual result should be:

```text
monetary source values
\&#x20;   â†’
can safely preserve up to the approved internal precision

rates
\&#x20;   â†’
may preserve calculation precision independently

display
\&#x20;   â†’
uses currency-aware formatting

final payable rounding
\&#x20;   â†’
remains a separate explicit future policy
```

The later payable-rounding decision still belongs to the later financial cutover.

G0.1 must not accidentally invent that policy.

\---

\## Why this must happen before P3b

The new Compensation system will create new monetary values.

It is a mistake to create new monetary architecture while an existing canonical monetary domain still violates the new currency contract.

The currency foundation should be internally coherent first.

\---

\# 7. G0.2 â€” Continuous Validation / CI Baseline

\## Why this exists

Flussra has become a large project.

The current backend test suite contains thousands of tests.

The frontend contains hundreds more.

There is a long migration chain.

The project now includes complicated concurrency, financial evidence, review, security, and effective-dating behavior.

At this size, relying only on locally reported validation is no longer sufficient.

The current GitHub repository does not contain a real automated pull-request CI workflow.

PR #30 itself documents that the full backend suite was run before four stale migration-head assertions were corrected.

Those four tests subsequently passed when rerun, and large focused validation cohorts also passed.

There is no evidence that the full backend suite was rerun after the final correction.

This does not prove that `main` is broken.

It means that the repository currently lacks a \*\*repeatable automated acceptance authority\*\*.

\---

\## Why this is architectural and not just developer convenience

In a payroll system, regressions can affect:

```text
permissions
currency
effective dates
financial calculations
finalization
immutable evidence
database migrations
concurrency
```

At this point CI is part of the correctness boundary.

The architecture is only as safe as our ability to prove that changes do not silently violate existing invariants.

\---

\## What G0.2 is intended to establish

There should be a repeatable clean validation path starting from the repository itself.

Conceptually:

```text
clean checkout

\&#x20;   â†“

fresh database

\&#x20;   â†“

migrate from baseline to current head

\&#x20;   â†“

backend tests

\&#x20;   â†“

frontend tests

\&#x20;   â†“

backend static validation

\&#x20;   â†“

frontend build/lint

\&#x20;   â†“

result visible on the PR
```

The important property is not a particular CI vendor or YAML shape.

The important property is:

> A change should not reach `main` merely because one developer or agent says that the important tests were run.

The repository should provide its own reproducible proof.

\---

\# 8. G0.3 â€” Currency Concurrency and Lock Ownership Cleanup

\## Why this exists

P3a had to solve a difficult race.

Imagine:

```text
Transaction A:
change Company currency USD â†’ EUR

Transaction B:
create new monetary data
```

If they race incorrectly, new financial history could be written under one denomination while the Company changes to another.

P3a correctly solved this by serializing monetary writers and currency changes through the Company authority.

That was a strong correctness choice.

However, the current mechanism has a scalability side effect.

Many ordinary monetary writes lock the Company row.

That means two unrelated payroll operations for different branches of the same company may serialize unnecessarily.

Conceptually:

```text
Company

Branch A payroll write
\&#x20;      â”‚
\&#x20;      â”œâ”€â”€ Company lock
\&#x20;      â”‚
Branch B payroll write waits
```

For a small tenant this may not matter.

For larger tenants it creates a company-wide contention point.

This is exactly the kind of assumption we should remove before production.

\---

\## Another problem discovered during this review

The broad Company serialization currently protects some unrelated concurrency behavior indirectly.

A good example is Status Key usage limits.

The current flow approximately performs:

```text
read/count current status usages

\&#x20;   â†“

check configured limit

\&#x20;   â†“

later write the Day Entry state
```

The Period mutation lock occurs later.

Normally this kind of count-then-write flow requires explicit serialization to prevent two requests from both observing available capacity.

The current Company-level currency lock effectively serializes the requests first.

Therefore the usage-limit race is currently protected partly because of a lock that belongs to another concern.

That is accidental coupling.

\---

\## Desired separation of responsibility

The future architecture should make each domain responsible for its own concurrency rule.

Conceptually:

```text
Currency denomination race
\&#x20;   â†’
Currency authority lock

Status usage-limit race
\&#x20;   â†’
Period / Status mutation lock

Payroll workflow sequencing
\&#x20;   â†’
Branch workflow lock

Target compensation assignment overlap
\&#x20;   â†’
Compensation-specific DB constraints / locking
```

This is clearer, safer, and more scalable.

\---

\## Important warning

The Company-wide serialization must not simply be removed for performance.

Doing so without relocating the concurrency protections it currently provides could introduce correctness bugs.

G0.3 therefore means:

> decouple concurrency responsibilities deliberately.

Not:

> remove locks.

\---

\## Architectural ownership issue

The Company currency module currently imports the Company locking primitive from Payroll Setup locking infrastructure.

The Company monetary-denomination boundary is no longer a Payroll Setup-specific concern.

Its locking primitive should conceptually belong to a neutral Company/currency/concurrency boundary.

Again, the exact code organization is implementation detail.

The architectural point is ownership.

\---

\# 9. G0.4 â€” Generic Period Pay Writer Retirement

\## What Generic Period Pay currently represents

The legacy payroll backend still supports generic period-level financial entries.

Conceptually:

```text
Driver
Payroll Period
Line Type
Amount
```

with generic mutation routes for adding/updating/voiding Period Pay rows.

This originally allowed multiple period-level financial concepts to share one broad mechanism.

\---

\## Why this is no longer desirable

The modern Flussra architecture is moving away from generic money buckets.

Important financial concepts increasingly have explicit domain ownership.

For example:

```text
Bonus
\&#x20;   â†’
PayrollBonusEvents

Minimum / Maximum
\&#x20;   â†’
DriverPayRules

Status
\&#x20;   â†’
DayEntryState + Status domain

Daily compensation
\&#x20;   â†’
PayDefinition / Compensation
```

That is a better architecture because the system knows what each amount means.

A generic period amount does not provide that clarity.

If Flussra later introduces:

```text
Commission

Allowance

Reimbursement

Manual Correction

Per Diem
```

the correct question should be:

> What domain owns this money, what rules apply to it, how is it audited, and how does it affect payroll?

Not:

> Can we insert it into generic Period Pay?

\---

\## Important distinction

The \*\*Period Pay report\*\* is not the same thing as the Generic Period Pay writer.

The read/report concept can remain useful.

For example, a report showing:

```text
daily pay
status pay
period contributions
bonus
min/max adjustment
total pay
```

is perfectly valid.

The undesirable part is the generic mutable monetary entry authority.

Therefore G0.4 targets the obsolete write authority, not the reporting concept.

\---

\## Why this belongs before P3b

P3b will establish the future compensation boundaries.

There is no value in allowing a broad anonymous financial writer to survive beside them if the product does not need it.

Every surviving monetary writer must be considered by:

```text
currency rules
auditing
snapshots
rounding
security
finalization
tests
```

Removing an obsolete writer reduces the permanent financial surface.

\---

\# 10. G0.5 â€” Predecessor Custom Pay Item Writer Retirement

\## Current situation

Flussra has gone through multiple generations of custom Pay Item creation.

The current product-facing custom-definition workflow is largely the CDPI domain.

It provides concepts such as:

```text
request
draft
submit
approve/reject
creator
approver
requesting branch
proposed item name
input type
unit
calculation method
events/history
direct company creation
branch activation
```

Those are meaningful product concepts.

However, older custom Pay Item write paths still exist in the backend.

Examples include older generic Pay Item creation/update and predecessor request/mapping APIs.

Some appear to have no current frontend product consumer.

\---

\## Why simply leaving them is harmful

If obsolete writers remain reachable, the future P3b/P3c architecture must either:

```text
support them

block them

test them

translate them

or explicitly exclude them
```

All of those have a cost.

A dead writer is still an API authority as long as it is reachable.

This is especially dangerous when new and old writers can create semantically similar definitions differently.

\---

\## What must NOT happen

G0.5 must not casually delete CDPI itself.

The current CDPI storage contains real business provenance.

For example:

```text
who requested a definition
which branch requested it
what method/input/unit were proposed
who approved it
when it was approved
whether it was direct-created
what event history occurred
```

The future generic PayDefinition governance will need to preserve the useful meaning of that information.

CDPI identity may disappear later.

Its useful governance semantics must not.

\---

\## G0.5 therefore means

Identify predecessor custom-definition mutation paths that:

```text
are not part of the current desired product
AND
have no required current runtime consumer
```

and retire those authorities before P3b.

The current CDPI governance remains temporarily until equivalent generic PayDefinition governance exists.

\---

\# 11. G0.6 â€” Pay Profile Family Dead-Authority Audit

\## Why this exists

The schema still contains an older Pay Profile family, including concepts such as:

```text
PayProfiles

PayProfilePayItems

PayProfileRates

PersonPayProfileAssignments
```

P3a had to include `PayProfileRates` in the Company durable-monetary-state logic.

That means the new currency invariant is now paying compatibility cost for this old model.

During the architecture review, these Pay Profile structures did not appear to own a meaningful current payroll runtime path in the modern system.

That makes them strong dead-schema candidates.

But this conclusion must be proven, not assumed.

\---

\## Why this matters before P3b

Imagine keeping PayProfileRates while building the new Compensation model.

Now Company currency logic must know about:

```text
legacy DriverRates
target DriverRateAssignments
PayProfileRates
Bonus
rules
draft money
snapshots
final lines
```

Every new financial invariant becomes more complicated.

If Pay Profiles no longer serve a desired product use, carrying them forward serves no purpose.

\---

\## Required decision logic

The question is not:

> Are these tables old?

The question is:

> Do they still have a legitimate current or approved future product responsibility?

If the answer is yes:

their purpose must be made explicit and incorporated into the target architecture intentionally.

If the answer is no:

they should be removed now while Flussra is pre-production.

Do not keep them indefinitely merely because migration `0001` created them.

\---

\# 12. What G0 Must Preserve

G0 is cleanup and contract closure.

It must not damage modern authorities that are already sound.

Important current authorities that remain include:

```text
Payroll Setup
Payroll Setup Versions
Branch Payroll Setup Assignments

Workforce / Employee
effective Driver profiles

Access / User / Employee link
Self scope

PayrollPeriods
PayrollPeriodDays

canonical DayEntryState / StatusKey

PayrollBonusEvents

DriverPayRules

period driver eligibility snapshots

calculation snapshots

review workflow

immutable snapshot evidence

finalization

PayrollFinalLines

finalized reporting/audit foundations

CDPI governance/provenance until its target replacement exists
```

G0 is not an excuse to reopen these architectures casually.

\---

\# 13. What G0 Explicitly Does NOT Build

G0 does not build the new Compensation domain.

It does not introduce:

```text
PayDefinitions

target RateDefinitions

RateComponentDefinitions

DriverRateAssignments

DriverRateValues

target resolver

target Payroll calculation

OrdinalTier target implementation

target Status compensation

transfer compensation copy

final Compensation UI
```

Those begin after G0.

G0 exists to give that work a cleaner starting point.

\---

\# 14. What G0 Does NOT Decide Yet

Several future financial decisions remain intentionally outside G0.

\## Payable rounding

G0 must not decide:

```text
round every line?

round every Driver?

round only final payable?

HALF\\\_EVEN?

HALF\\\_UP?

residual handling?
```

That requires a deliberate payroll-product decision later.

G0 only ensures that storage and inputs are capable of preserving the required precision until that decision exists.

\---

\## Multi-currency payroll

V1 remains:

```text
one Company = one CurrencyCode
```

No FX engine is introduced.

No per-rate currencies are introduced.

\---

\## Generic compensation subject

V1 remains driver/fleet payroll.

No generic:

```text
EmployeeCompensationSubject
```

abstraction is required.

\---

\## Formula engine

No expression language or universal payroll rules engine is introduced.

\---

\# 15. Relationship Between G0 and P3b

Once G0 is closed, P3b can begin against a cleaner repository.

P3b is expected to answer a fundamentally different question:

> What should Flussra's future Compensation domain look like?

The current architectural direction after review is likely to include clean target concepts such as:

```text
PayDefinition

PayDefinition Branch applicability

RateDefinition

RateComponentDefinition

DriverRateAssignment

DriverRateValue
```

without operational dependence on:

```text
RateType
PayItemRateTypeMap
PayItemRateSlot
```

But those are P3b concerns.

G0 should not prematurely implement them.

\---

\# 16. Why We Are Not Simply Following the Existing Plan

The existing architecture plans are valuable.

They capture many correct decisions.

They are not sacred.

The repository has evolved substantially since earlier plans were written.

For example, Flussra has since established:

```text
canonical Payroll Setup authority
retirement of legacy payroll schedule authority
Workforce refoundation
Access/Self refoundation
Company Currency authority
```

Therefore plans must be continually revalidated against current code.

The goal is not:

> complete every old phase exactly as originally written.

The goal is:

> produce the strongest payroll architecture possible before production.

If current evidence shows that a previous plan should be simplified, reordered, or changed, change the plan.

Do not protect planning documents from reality.

\---

\# 17. G0 Closure Criteria

G0 should not be considered complete merely because six tickets were merged.

It is complete when the repository demonstrates a coherent post-P3a boundary.

At minimum, the following must be true.

\## Currency precision

The currently supported monetary domains no longer contain an accidental two-decimal-only canonical storage contract that contradicts supported Company currencies.

Values requiring up to the chosen internal precision can survive correctly.

No change in G0 silently establishes final payable-rounding semantics.

\---

\## Currency concurrency

Changing Company currency and creating durable monetary state cannot race into inconsistent denomination history.

At the same time, ordinary unrelated financial writes should not require unnecessary Company-wide serialization merely because they belong to different branches.

Any correctness that previously depended accidentally on the Company lock must have an explicit owner.

\---

\## Status limit concurrency

Concurrent same-period writes cannot exceed configured Status usage limits simply because a broad currency lock was narrowed.

The domain owns its own serialization.

\---

\## Legacy monetary writers

Generic Period Pay mutation authority is no longer an alternate broad financial entry path if the product has no requirement for it.

Its reporting/read meaning may remain where useful.

\---

\## Old custom-definition writers

Only the currently desired custom-definition/governance authority remains reachable.

Predecessor write paths with no legitimate product consumer are gone.

CDPI provenance required for future generic governance remains preserved.

\---

\## Pay Profiles

The Pay Profile family has an explicit outcome:

```text
either

A) proven product responsibility and retained intentionally

or

B) proven dead and removed
```

There should be no third answer:

```text
"we were not sure, so we left it forever."
```

\---

\## Validation authority

A clean repository can prove itself reproducibly.

The expected baseline includes:

```text
fresh database migration

backend full suite

frontend tests

backend lint/static validation

frontend lint/build

migration-head correctness
```

and that validation should be part of the repository's normal merge discipline.

\---

\# 18. The Desired State at the End of G0

The repository should conceptually look like this:

```text
COMPANY
\&#x20; Currency authority
\&#x20;     âœ“ coherent precision contract
\&#x20;     âœ“ correct concurrency boundary

PAYROLL
\&#x20; Payroll Setup
\&#x20;     âœ“ canonical

\&#x20; Period lifecycle
\&#x20;     âœ“ canonical

\&#x20; Daily state
\&#x20;     âœ“ canonical

\&#x20; Bonus
\&#x20;     âœ“ canonical

\&#x20; Min/Max rules
\&#x20;     âœ“ canonical

\&#x20; Review/finalization/evidence
\&#x20;     âœ“ canonical

LEGACY COMPENSATION
\&#x20; PayItems / RateTypes / DriverRates
\&#x20;     still temporarily authoritative

\&#x20; BUT:
\&#x20;     unnecessary side writers removed
\&#x20;     dead financial roots removed
\&#x20;     no generic Period Pay mutation escape hatch
\&#x20;     no obsolete predecessor custom writers

TARGET COMPENSATION
\&#x20;     not implemented yet
```

That is the ideal handoff into P3b.

\---

\# 19. Why We Are Not Deleting All Legacy Compensation in G0

Because replacement authority does not exist yet.

The current payroll still needs legacy:

```text
PayItems
RateTypes
DriverRates
DriverRateTiers
rate mappings / slots
```

for operational payroll calculation.

Deleting those now would require either:

```text
a big-bang Compensation rewrite

or

temporary duplicate architecture
```

Neither is desirable.

The safe order remains:

```text
remove proven-dead side authorities

\&#x20;       â†“

build target Compensation

\&#x20;       â†“

prove target

\&#x20;       â†“

cut operational authority

\&#x20;       â†“

delete the replaced legacy core promptly
```

G0 removes dead weight.

P3b/P3c build the replacement.

P4/P5 remove the remaining legacy authority as its replacement becomes real.

\---

\# 20. Final Architectural Intent

G0 exists because P3a did more than add a currency field.

It exposed where Flussra still carries assumptions from older architecture.

The correct response is not to immediately begin adding more tables.

The correct response is to close those exposed contracts first.

The intended sequence is:

```text
P3a
Company Currency Authority
âœ… CLOSED

\&#x20;       â†“

G0
POST-CURRENCY CONTRACT CLOSURE

\&#x20;   1. Monetary precision coherence
\&#x20;   2. Automated validation / CI authority
\&#x20;   3. Correct and scalable currency concurrency boundary
\&#x20;   4. Remove generic Period Pay mutation authority
\&#x20;   5. Remove predecessor custom Pay Item writers
\&#x20;   6. Prove and remove dead Pay Profile architecture if appropriate

\&#x20;       â†“

G0 CLOSED

\&#x20;       â†“

P3b
Clean Target Compensation Domain
```

The governing rule throughout G0 is:

> Flussra is pre-production. Preserve business meaning, financial correctness, audit meaning, security boundaries, and sound architecture. Do not preserve obsolete implementation merely because it already exists.

And equally:

> Do not introduce future complexity simply because large enterprise systems sometimes need it. Build clean foundations for the product Flussra actually needs, while avoiding design choices that unnecessarily block future scale.

G0 should leave P3b with fewer authorities, fewer legacy assumptions, fewer financial writers, a coherent currency contract, explicit concurrency ownership, and a repository capable of proving its own correctness.

That is the reason for this stage.
