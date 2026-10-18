# Branch Timezone and Temporal Authority Contract

**Status:** Implemented cutover contract for migration `0086`; see the companion evidence report for baseline, schema, verification and remaining P4c gate.

**Scope:** Branch-local business dates, instant-based Driver assignments/transfers, and frozen payroll eligibility WorkDates.
**Supersession:** This document amends temporal assumptions in the People / Workforce / Access contract and its older master plan. It does not change payroll formulas, rate/setup effective DATEs, payroll period dates, termination/hire DATEs, WorkDate, event timestamps, or the Unified Plan's P4c evidence/finalization gate.

## Authority map

| Domain | Authority | Interpretation |
|---|---|---|
| Branch timezone | `core.Branches.TimeZoneId` | Required IANA/PostgreSQL regional identifier or UTC; exact identity is selected from `core.SupportedTimeZones`. No Company timezone/default exists. |
| Current business date | `core.fn_BranchToday(company, branch, instant)` | Tenant-scoped Branch lookup and canonical instant-to-date conversion. Missing Branch returns NULL; never falls back to Company or host/browser time. |
| Prospective Branch date | `core.fn_BusinessDate(time_zone_id, instant)` | Same SQL conversion before Branch persistence; invalid identifiers fail. Python helpers call SQL and do no timezone arithmetic. |
| Current Driver identity | `core.fn_EffectiveDriverProfile(company, employee, instant)` | Instant range `[EffectiveFromAt, EffectiveToAt)` determines current profile; SQL `NOW()` is the default transaction instant. |
| Payroll attribution | `BranchID + WorkDate` | A Driver assignment is eligible for that WorkDate when its instant range overlaps that Branch's entire local calendar day. Transfers do not create, split, prorate, or copy pay. Only explicit payroll inputs calculate compensation. |
| Frozen eligibility | `EligibleWorkDates DATE[]` and `BranchTimeZoneIdSnapshot` | Capture-time facts. Existing arrays and timezone snapshots never change when a Branch timezone changes. Current assignment bounds may still restrict new source inputs using the captured timezone's local-day boundary. |
| Payroll configuration | Setup/Version effective DATEs, rates, periods and planned dates | Company-owned chronology or explicit input dates; no inherent Company “today.” Contextual version reads may use a selected Branch's business date; without context `is_current` and reference date are NULL. |
| Employment and payroll inputs | HireDate, TerminationDate, WorkDate, period dates, rate/setup dates | Remain DATE values. No implicit timezone reinterpretation. |
| Audit and event history | `TIMESTAMPTZ` | Absolute event instants; browser timezone affects display only. JWT/host metadata instants remain valid. |

## Timezone registry and date conversion

Migration `0086` seeds `core.SupportedTimeZones` from PostgreSQL's installed `pg_timezone_names` catalog using the UTC/regional-prefix allowlist. The table is the DB-supported identity registry and FK authority for Branch zones and captured zones. PostgreSQL's timezone catalog and tzdata perform conversions; there is no Python `ZoneInfo`, static client list, runtime Company fallback, or TTL cache. Timezone conversion and local-day-to-instant boundary calculation stay in SQL; ordinary DATE chronology and ISO-date navigation remain pure DATE operations. A later tzdata refresh may add newly supported catalog identifiers with `INSERT ... ON CONFLICT DO NOTHING`; referenced identities are not deleted.

`core.fn_BusinessDate` is the only operational UTC-instant-to-business-date conversion. `core.fn_BranchToday` is tenant-scoped and never falls back. `core.fn_LocalDateBoundary` is the canonical local-midnight boundary resolver used by `core.fn_WorkDateRange`, including midnight folds; `core.fn_WorkDateRange` converts an inclusive DATE span to a half-open instant range by converting the next local calendar date before timezone conversion. The SQL boundary path handles 23/25-hour days, repeated midnight hours and skipped local dates without adding fixed UTC durations.

Branch timezone is editable and audited as normal Branch configuration. Updating it affects future/current clock evaluation but does not rewrite any DATE, event instant, frozen eligibility timezone, or frozen WorkDate array. No timezone history/effective-dating/default abstraction is introduced.

## Instant assignment and transfer

Driver assignment history is stored only as half-open instant ranges. DATE-based Driver `EffectiveFrom`/`EffectiveTo`, DATE resolver overloads and DATE range functions are retired rather than retained as parallel authority. Transfers switch source and destination at one exact `EffectiveAt TIMESTAMPTZ`; immediate requests use database `NOW()` at completion, while explicit scheduled transfers must remain future at creation/completion. Source close and destination open use the identical instant. A future completed destination remains pending until that instant.

Current Driver identity, Employee Branch projection, administrative current-linked Driver views and Driver Self authorization resolve against the same instant resolver. Historical payroll eligibility is WorkDate-based and must not cast an instant to a DATE. Termination remains an explicit DATE and caps each profile in its own Branch at `core.fn_LocalDateBoundary(profile_branch_zone, TerminationDate + 1)`, including a skipped local date. Empty future assignments may be retained only as empty Terminated profiles; they grant no new payroll-source or Bonus authority.

Branch settings people counts use the same instant projection policy in a read-only query. They count each Employee under the current Driver Branch when one resolves, otherwise retain the stored Branch for non-Driver employees or the latest ended nonempty Driver Branch for a terminated Employee. Reading metrics does not synchronize or lock Employee rows.

An assignment may cover both source and destination Branch local days around a midday transfer. That permits explicit WorkDate/source entries in both Branches where the instant ranges overlap each local day; the transfer itself never generates financial rows. Half-open bounds mean an assignment ending exactly at the next local day's start does not cover that next day.

## Frozen eligibility and lifecycle

Every valid PayrollPeriod has its tenant-scoped eligibility marker. A missing marker is structurally invalid and fails closed; reads never rebuild or fall back to a live roster. Capture materializes the period's calendar DATEs using SQL integer offsets and stores exactly those WorkDates whose Branch-local day intersects the Driver assignment, subject to employment DATE gates. Frozen rows and arrays are immutable after capture, including when a Branch timezone changes. The existing Draft-to-Open promotion intentionally regenerates provisional roster rows and freezes the resulting capture. `IncludedByExistingData` remains an exact-date rescue classification and does not authorize new period Bonus entries.

An explicit Workforce profile creation or transfer completion may append only the new destination/profile roster row to source-editable periods (`Draft`, `Open`, `Returned`) that already have a marker. Capture reuses the canonical snapshot SQL with an only-Driver filter; existing rows and frozen date arrays are untouched. Review states stay immutable. Lock order is Branch workflow advisory lock followed by PayrollPeriod row locks in ascending ID; the helper takes neither Employee nor currency/compensation locks.

New payroll source checks intersect the live assignment instant range with the frozen Branch timezone's WorkDate range, in addition to existing employment/hire/termination/status rules. Captured timezone/date authority is immutable even if Branch timezone is edited later. A new Bonus requires at least one captured WorkDate that still overlaps the live nonempty assignment. Existing exact-date saved-source rescue remains unchanged.

## Clean cutover and evidence gate

Migration `0086` refuses before DDL when any Driver profile, DriverTransferRequest, or PayrollPeriod exists (`BRANCH_TIMEZONE_REBUILD_REQUIRED`). The old DATE history cannot be truthfully translated into instants and old frozen payroll evidence cannot be rewritten. Rebuild the preproduction database or explicitly correct invalid legacy Branch timezone configuration before retrying; the migration does not invent transfer times or default to UTC. Branches without Driver history and Employees without profiles may remain.

This refoundation does not complete P4c. Target payroll submit, resubmit, finalization, finalization preview and immutable evidence remain blocked under the existing `TARGET_PAYROLL_EVIDENCE_NOT_READY` gate until the Unified Refoundation Plan's P4c requirements are met.

## References

- [PostgreSQL date/time types and timezone behavior](https://www.postgresql.org/docs/18/datatype-datetime.html)
- [PostgreSQL `pg_timezone_names` catalog](https://www.postgresql.org/docs/18/view-pg-timezone-names.html)
- [Implementation evidence and verification](../evidence/BRANCH_TIMEZONE_REFOUNDATION.md)
