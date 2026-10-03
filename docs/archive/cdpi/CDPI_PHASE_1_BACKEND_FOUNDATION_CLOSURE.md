# CDPI Phase 1 Backend Foundation — Closure Report

**Status: ACCEPTED AND CLOSED**

---

## 1. Executive Summary

Phase 1 Backend Foundation for Custom Daily Pay Items (CDPI) is accepted and closed.

| Item | Value |
|---|---|
| Baseline commit | `adb77b7` |
| Final Task 7 commit | `fff434f` |
| Hygiene cleanup commit | `0d3d358` |
| Current Alembic head | `0045` |

**Closure validation results:**

| Suite | Result |
|---|---|
| Explicit CDPI suite (closure review) | 260 passed |
| Full backend suite (closure review) | 1917 passed, 4 skipped |
| Hygiene validation (all CDPI) | 150 passed |

Later runs of the CDPI suite show lower counts (e.g. 150) depending on which test files are selected. The closure review ran the broader explicit CDPI suite; those counts are the accepted baseline.

---

## 2. Architecture Accepted

### Pre-approval workflow

`payroll.CdpiRequests` is the pre-approval workflow container. Draft, Pending, and Rejected requests are workflow-only records — they do not create real PayItems, PayItemRateTypeMap, RateTypes, BranchPayItemConfig, or any payroll-visible rows.

### Approval / Direct creation

Approval and direct company creation both produce real `payroll.PayItems` rows. Each PayItem created this way receives exactly one `payroll.CdpiDefinitions` marker row, which locks it as a CDPI-managed item.

### CdpiDefinitions

`payroll.CdpiDefinitions` has **no** `SourceRequestID` column after migration `0044`. The canonical link between a CDPI request and its resulting PayItem is:

```
CdpiRequests.ApprovedPayItemID -> PayItems.PayItemID
```

### Company-level immutability

Once a PayItem is approved or directly created, its company-level structure (Name, DataType, ItemScope, RateBehavior, CalcMethodKey) is immutable. No CDPI branch endpoint may alter these fields.

### Branch-level control

Branches may only control:

- **IsActive** — activate or deactivate the item for that branch
- **BranchDisplayName** — a display-name override stored in `BranchPayItemConfig.BranchDisplayName`

The override does **not** rewrite `PayItems.Name`.

---

## 3. Completed Tasks

### Task 1 — Database Foundation

| | |
|---|---|
| Commits | `d89bd05` (original), `e849d07` (correction) |
| Migrations | `0043`, `0044` |
| Files | `migrations/sql/0043_*`, `migrations/sql/0044_*`, `migrations/versions/0043_*`, `migrations/versions/0044_*` |

Created the full CDPI schema: `CdpiRequests`, `CdpiRequestEvents`, `CdpiDefinitions`. Migration `0044` applied corrections including removal of `SourceRequestID` from `CdpiDefinitions`, addition of the `ck_cdpirequests_approvallink` check constraint (status='Approved' iff ApprovedPayItemID IS NOT NULL), and index/trigger refinements.

### Task 2 — Contracts + Permission Guards

| | |
|---|---|
| Commits | `fcd9678`, `a2599e5` (hygiene) |
| Files | `backend/app/cdpi/schemas.py`, `backend/app/cdpi/guards.py` |

Defined all Pydantic schemas (`CdpiRequestSummary`, `CdpiRequestDraftFields`, `CdpiRequestCreate`, `CdpiRequestUpdate`, `CdpiSubmitRequest`, `CdpiDecideRequest`, `CdpiDecideAction`). Implemented permission guards: `require_cdpi_branch_edit` (branch-scoped payitems.edit) and `require_cdpi_company_edit` (AllCompanyBranches payitems.edit).

### Task 3 — Draft CRUD + Optimistic Concurrency

| | |
|---|---|
| Commits | `1a96588`, `878ebff` (correction) |
| Files | `backend/app/cdpi/service.py`, `backend/app/cdpi/router.py` |

`POST /settings/cdpi/requests` — create Draft.
`GET /settings/cdpi/requests` — list (filtered by status/branch for caller scope).
`GET /settings/cdpi/requests/{id}` — read single request.
`PATCH /settings/cdpi/requests/{id}` — update Draft with optimistic concurrency (`expected_revision`).

All writes use `sqlalchemy.text()` parameterized queries. Optimistic concurrency pattern: `UPDATE ... WHERE status='Draft' AND revision=expected RETURNING ...`; zero rows → 409.

### Task 4 — Submit / Return / Reject / Copy

| | |
|---|---|
| Commits | `57a7ab4`, `50ad300` (correction) |
| Files | `backend/app/cdpi/service.py`, `backend/app/cdpi/router.py` |

`POST /settings/cdpi/requests/{id}/submit` — validates completeness, transitions Draft → PendingCompanyApproval, inserts Submitted/Resubmitted event.
`POST /settings/cdpi/requests/{id}/decide` — routes ReturnToDraft / Reject / Approve.
`POST /settings/cdpi/requests/{id}/copy` — copies a Rejected request to a new Draft with `CopiedFromRequestID` set; inserts CopiedFromRejected event.

### Task 5 — PerUnit Method Adapter / Registry

| | |
|---|---|
| Commit | `993ac54` |
| Files | `backend/app/cdpi/methods.py`, `backend/app/cdpi/service.py`, `backend/tests/test_cdpi_methods.py` |

Defined `CdpiMethodAdapter` protocol. Registered PerUnit adapter: validates completeness (item_name, input_type='Time'|'Number', calc_method_key='PerUnit'), validates submit readiness. Future methods (OrdinalTier, Block, RangeBracket, RangeProgressive) are registered as stubs that raise NotImplementedError.

### Task 6 — Approval + Direct Company Creation

| | |
|---|---|
| Commits | `3981f58`, `f6b68b8` (DataType fix) |
| Files | `backend/app/cdpi/service.py`, `backend/app/cdpi/router.py`, `backend/app/cdpi/schemas.py`, `backend/tests/test_cdpi_approval.py` |

**Approval** (`action='Approve'` on decide endpoint): atomically creates PayItems + CdpiDefinitions + BranchPayItemConfig (requesting branch, IsActive=TRUE) + sets CdpiRequests.ApprovedPayItemID + inserts Approved event. All in one `engine.begin()` transaction. DataType is mapped from InputType: Time→Time, Number→Decimal.

**Direct company creation** (`POST /settings/cdpi/direct-company-items`): creates PayItems + CdpiDefinitions only. No BranchPayItemConfig rows (item starts inactive for all branches). Requires AllCompanyBranches scope.

Neither path creates RateTypes, PayItemRateTypeMap, PayItemSettings, or Pay Rate rows.

**Acceptance notes:** The `ck_cdpirequests_approvallink` check constraint requires status and ApprovedPayItemID to be set atomically in a single UPDATE. The service satisfies this with a single `UPDATE cdpirequests SET status='Approved', approvedpayitemid=:pid WHERE ...`.

### Task 7 — Branch Controls + Branch Display Name Override

| | |
|---|---|
| Commits | `471a5bc`, `5d19e34` (period-protection fix), `fff434f` (pending-row dating fix) |
| Files | `backend/app/cdpi/service.py`, `backend/app/cdpi/router.py`, `backend/app/cdpi/schemas.py`, `backend/tests/test_cdpi_branch.py`, `migrations/sql/0045_*`, `migrations/versions/0045_*` |

**Migration 0045:** Added `BranchDisplayName VARCHAR(200) NULL` to `payroll.BranchPayItemConfig`.

`GET /settings/cdpi/branches/{branch_id}/items` — lists all CDPI PayItems (those with a CdpiDefinitions row) with per-branch active state and display-name override. Non-CDPI PayItems are excluded via INNER JOIN on CdpiDefinitions.

`PATCH /settings/cdpi/branches/{branch_id}/items/{pay_item_id}` — updates IsActive and/or BranchDisplayName for a specific branch. Empty string or null clears the override. Only fields present in `model_fields_set` are applied (explicit null vs. omitted is distinguished).

**Effective-dating convention (three cases):**

| Open row `EffectiveFrom` vs resolved date | Action |
|---|---|
| Before resolved date | Close at `resolved − 1 day`, INSERT new open row |
| Equal to resolved date | UPDATE in place, EffectiveFrom unchanged |
| After resolved date (pending) | UPDATE in place, EffectiveFrom reset to resolved date |

Period protection: if an open payroll period covers today, the resolved effective date is pushed to `period_end + 1 day`. Current-period config rows are never mutated in place.

### Hygiene Cleanup

| | |
|---|---|
| Commit | `0d3d358` |
| Files | `backend/app/cdpi/service.py`, `backend/tests/test_cdpi_approval.py` |

Removed two stale inline comments that referenced `SourceRequestID=NULL` / `SourceRequestID = request UUID`. No runtime behavior changed.

---

## 4. Database / Migrations

### Migrations

| Migration | Description |
|---|---|
| `0043` | CDPI foundation: creates `CdpiRequests`, `CdpiRequestEvents`, `CdpiDefinitions` tables with initial indexes and triggers |
| `0044` | CDPI corrections: removes `SourceRequestID` from `CdpiDefinitions`, adds `ck_cdpirequests_approvallink` check constraint, adds `ApprovedPayItemID` FK, refines indexes and immutability triggers |
| `0045` | Adds `BranchDisplayName VARCHAR(200) NULL` to `payroll.BranchPayItemConfig` |

### Tables Involved

**`payroll.CdpiRequests`**
Pre-approval workflow container. Key columns: `RequestID` (PK UUID), `CompanyID`, `RequestingBranchID`, `ItemName`, `InputType`, `Unit`, `CalcMethodKey`, `Notes`, `Status`, `Revision`, `ApprovedPayItemID` (FK to PayItems — NULL until Approved), `CopiedFromRequestID`, submitted/created/updated audit columns.
Check constraint: `ck_cdpirequests_approvallink` — status='Approved' iff ApprovedPayItemID IS NOT NULL.

**`payroll.CdpiRequestEvents`**
Immutable audit log. Columns: `EventID`, `RequestID` (FK), `EventType`, `FromStatus`, `ToStatus`, `ActorUserID`, `RequestRevision`, `EventTimestampUtc`. Insert-only (update/delete blocked by trigger).

**`payroll.CdpiDefinitions`**
Marks a PayItem as CDPI-managed. Columns: `PayItemID` (PK, FK to PayItems), `DefinitionSchemaVersion INT DEFAULT 1`, `LockedAtUtc`, `CreatedByUserID`. No `SourceRequestID`.

**`payroll.PayItems`**
Company-level pay item record. CDPI items have: `CompanyID` = company, `BranchID` = NULL, `Category` = 'Custom', `ItemScope` = 'Daily', `RateBehavior` = 'PerUnit', `IsDefaultBranchActive` = FALSE, `IsSystemStandard` = FALSE. `DataType` = 'Time' (InputType=Time) or 'Decimal' (InputType=Number). `PayItemCode` = `CDPI{uuid_hex[:12].upper()}`.

**`payroll.BranchPayItemConfig`**
Effective-dated branch-level config. Key columns: `ConfigID`, `CompanyID`, `BranchID`, `PayItemID`, `IsActive`, `EffectiveFrom DATE NOT NULL`, `EffectiveTo DATE NULL`, `BranchDisplayName VARCHAR(200) NULL` (added in 0045), `CreatedByUserID`. Unique index `uix_BranchPayItemConfig_OpenVersion` on (CompanyID, BranchID, PayItemID) WHERE EffectiveTo IS NULL ensures at most one open row per branch+item.

---

## 5. API Surface Added

All routes are mounted at `/settings/cdpi`.

### CDPI Request Workflow

| Method | Path | Description |
|---|---|---|
| `POST` | `/requests` | Create a new Draft CDPI request |
| `GET` | `/requests` | List requests visible to the caller (filtered by status/branch) |
| `GET` | `/requests/{id}` | Read a single request |
| `PATCH` | `/requests/{id}` | Update a Draft (optimistic concurrency via `expected_revision`) |
| `POST` | `/requests/{id}/submit` | Submit Draft → PendingCompanyApproval |
| `POST` | `/requests/{id}/decide` | Company action: ReturnToDraft / Reject / Approve |
| `POST` | `/requests/{id}/copy` | Copy a Rejected request to a new Draft |

### Company-Level Creation

| Method | Path | Description |
|---|---|---|
| `POST` | `/direct-company-items` | Create CDPI PayItem directly (bypasses request workflow) |

### Branch Controls

| Method | Path | Description |
|---|---|---|
| `GET` | `/branches/{branch_id}/items` | List CDPI items with branch-level active state and display-name override |
| `PATCH` | `/branches/{branch_id}/items/{pay_item_id}` | Update branch active state and/or display-name override |

---

## 6. Workflow Lifecycle

```
                     [create]
                        │
                      Draft  ◄─────────────── ReturnedToDraft
                        │                           ▲
                    [submit]                        │
                        │                     [decide: return]
                        ▼                           │
              PendingCompanyApproval ───────────────┘
                  │           │
      [decide:    │           │ [decide:
        approve]  │           │   reject]
                  ▼           ▼
              Approved     Rejected
                               │
                           [copy]
                               │
                             Draft (new RequestID, CopiedFromRequestID set)
```

**Lifecycle rules:**

- **Draft** — editable, may be incomplete. Fields: ItemName, InputType, CalcMethodKey (all required for submit), Unit, Notes (optional).
- **Submit** — requires complete PerUnit definition (all three required fields present and valid). Only the requesting branch can submit. Transitions to PendingCompanyApproval.
- **PendingCompanyApproval** — immutable except company reviewer actions. Requesting branch cannot alter it.
- **ReturnToDraft** — returns to Draft status on the same RequestID. Resubmit inserts a Resubmitted event.
- **Rejected** — terminal status for the request. No further edits or submissions.
- **Copy** — creates a brand-new Draft with `CopiedFromRequestID` pointing to the rejected request. Inserts CopiedFromRejected event on the new request.
- **Approve** — creates exactly one PayItem + CdpiDefinitions + BranchPayItemConfig (requesting branch, active). Sets ApprovedPayItemID. Inserts Approved event. Cannot be undone through the CDPI API.
- **Direct creation** — bypasses the request workflow entirely. Creates PayItem + CdpiDefinitions; no BranchPayItemConfig (all branches start inactive). Requires AllCompanyBranches scope.

---

## 7. Permissions Model

All CDPI write behavior requires the `payitems.edit` permission.

| Scope required | Action |
|---|---|
| Branch-scoped `payitems.edit` on the requesting branch | Create Draft, update Draft, submit, copy |
| AllCompanyBranches `payitems.edit` | Decide (approve/return/reject), direct company creation |
| Branch-scoped `payitems.edit` on target branch | Branch list, branch PATCH |

Scope types: `SpecificBranch` or `AllCompanyBranches`. Branch-scoped guards accept either type when the target branch matches. Company-level guards require `AllCompanyBranches` only.

Permissions `payroll.entry`, `setup.manage`, and all unrelated permissions are not sufficient for any CDPI action.

Cross-branch and cross-company leakage protections were reviewed and accepted:

- Branch ownership (`core.branches.CompanyID`) is verified before any branch action.
- PayItem company ownership is verified on every branch PATCH.
- Non-CDPI PayItems are rejected by the branch endpoints (no CdpiDefinitions row → 404).

---

## 8. Method Support

### Implemented

**PerUnit** — one scalar value per unit of input.

| InputType | DataType stored in PayItems |
|---|---|
| `Time` | `Time` |
| `Number` | `Decimal` |

`Unit` is optional display metadata (e.g. "km", "hours") and does not affect calculation.

### Registered but not implemented

| Method | Status |
|---|---|
| OrdinalTier | Registered stub — raises NotImplementedError |
| Block | Registered stub — raises NotImplementedError |
| RangeBracket | Registered stub — raises NotImplementedError |
| RangeProgressive | Registered stub — raises NotImplementedError |

No Pay Rates, RateTypes, PayItemRateTypeMap, PayItemSettings, or calculation behavior exists for any method yet.

---

## 9. Branch Controls

### Activation state

- Approval creates a BranchPayItemConfig row for the **requesting branch only**, with `IsActive = TRUE`.
- Direct company creation creates **no** BranchPayItemConfig rows; the item starts inactive for all branches.
- Branches activate or deactivate a CDPI PayItem via `PATCH /branches/{id}/items/{pid}`.

### Display-name override

- `BranchDisplayName` in `BranchPayItemConfig` stores an optional branch-specific label.
- Setting `branch_display_name_override` to a non-empty string stores the override.
- Setting it to `null` or `""` (empty after strip) clears the override; the branch falls back to `PayItems.Name`.
- `PayItems.Name` is never rewritten by branch endpoints.

### Effective-dating

Config writes follow the existing settings convention:

| Condition | Behavior |
|---|---|
| No existing open row | INSERT fresh row at resolved `effective_from` |
| Open row `EffectiveFrom` < resolved date | Close existing at `resolved − 1 day`, INSERT new open row |
| Open row `EffectiveFrom` == resolved date | UPDATE in place, date unchanged |
| Open row `EffectiveFrom` > resolved date (pending) | UPDATE in place, reset `EffectiveFrom` to resolved date |

**Period protection:** If an open payroll period covers today, `effective_from` is pushed to `period_end + 1 day`. Rows belonging to a protected period are never mutated in place.

**Uniqueness:** `uix_BranchPayItemConfig_OpenVersion` ensures at most one open row per (CompanyID, BranchID, PayItemID) at all times.

---

## 10. Explicit Exclusions — Not Built Yet

The following were explicitly excluded from Phase 1 and do not exist in any form:

- Frontend UI (no CDPI pages, forms, or components)
- Pay Rates integration (no rates created on approval or direct creation)
- `RateTypes` rows for CDPI PayItems
- `PayItemRateTypeMap` rows for CDPI PayItems
- `PayItemSettings` rows for CDPI PayItems
- Payroll entry integration (CDPI items are not yet usable in payroll runs)
- Calculation engine support for CDPI items
- Finalization support
- Ledger integration
- OrdinalTier implementation
- Block implementation
- RangeBracket implementation
- RangeProgressive implementation
- Method-specific DB config tables (none created)
- Customer-facing operator wizard UI

---

## 11. Known Non-Blocking Notes

- **Stale SourceRequestID comments** — cleaned in hygiene commit `0d3d358`. No further stale references remain in service or test files.
- **pytest cache warning** — non-blocking; arises from test runner environment, not from CDPI code.
- **git LF/CRLF warning** — environmental; Windows line-ending normalization on commit. Non-blocking.
- **Driver transfer test** — a previously observed unrelated driver transfer test issue was rechecked during Phase 1 closure; the final full backend suite passed.

---

## 12. Recommended Next Phase

### Recommended: Frontend CDPI Workflow UI first

The backend workflow is complete and validated. Operators cannot currently use it without a frontend. The practical next step is:

1. **CDPI request workflow UI** (branch side):
   - Create Draft form (ItemName, InputType, Unit, CalcMethodKey)
   - Edit Draft
   - Submit for company approval
   - View request status / history

2. **CDPI company review queue** (company admin side):
   - List pending requests
   - View request details
   - Return to Draft with reason
   - Reject with reason
   - Approve (triggers PayItem creation)
   - Copy rejected to new Draft

3. **Branch activation / display-name controls UI**:
   - List CDPI items per branch
   - Toggle active/inactive
   - Set/clear display-name override

4. **Pay Rates integration** (after UI is usable):
   - Once operators can approve and manage CDPI items, connect PerUnit items to the Pay Rates matrix (RateTypes, PayItemRateTypeMap, driver rate assignment).
   - Enable payroll entry to pick up CDPI PerUnit hours/units from daily entry.

Frontend first is practical because the backend is verified and stable, but zero operator value is delivered until the UI exposes it.

---

*Report generated after hygiene commit `0d3d358` on branch `custom-daily-refactor`.*
