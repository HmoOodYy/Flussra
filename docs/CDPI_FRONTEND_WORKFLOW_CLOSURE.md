# CDPI Frontend Integration — Closure Report

**Branch:** `custom-daily-refactor`
**Through commit:** `4574ae0`
**Date:** 2026-06-16

---

## 1. Executive Summary

Frontend CDPI Integration phases FE-1 through FE-5 are accepted. The full
branch-request → company-review → approval → branch-activation lifecycle is
now wired to the accepted CDPI backend. Branch managers can submit custom
daily pay-item requests; company-wide managers can approve, return, or reject
them; approved items can be activated per branch with an optional display-name
override. All mutations use CDPI-specific endpoints — the old
`/settings/pay-items` mutation path is no longer called by the wizard.
Effective-dated backend semantics are respected: the UI refetches after each
update and shows scheduled-period messaging when the returned state does not
match the requested value.

---

## 2. Accepted Commits / Milestone List

| Commit    | Phase              | Description |
|-----------|--------------------|-------------|
| `2c92b83` | FE-1               | CDPI API types, API client module (`cdpiApi.ts`), permission helpers |
| `71a7064` | FE-1 correction    | Null safety + conservative scope check in permission helpers |
| `f0168a7` | FE-2               | Daily Pay Items route/nav accessible to `payitems.edit` users |
| `682d930` | FE-3               | Add Custom Item wizard converted to CDPI PerUnit-only flow |
| `9c6853a` | FE-4               | CDPI request review section (Approve / Return / Reject) |
| `c376f9c` | FE-4 correction    | Required non-empty reason for all three decide actions |
| `033a9db` | FE-5               | Branch CDPI active/display-name controls |
| `4574ae0` | FE-5 correction    | Effective-dated-safe refetch and messaging |

---

## 3. Current User-Facing Workflow

### 3.1 Branch request creation and submission

A branch manager with `payitems.edit` scoped to their branch opens
**Settings → Daily Pay Items**, selects their branch, and clicks
**+ Add Custom Item**. The three-step wizard (Value Type → How It Works →
Name It) collects:

- `item_name` — the official pay-item name
- `input_type` — `Time` or `Number` (wizard step 1)
- `calc_method_key` — always `PerUnit` (only enabled method)
- `unit` — optional display label
- `notes` — optional internal note

On submit the frontend calls:

1. `POST /settings/cdpi/requests` → creates a draft
2. `POST /settings/cdpi/requests/{id}/submit` → moves to `PendingCompanyApproval`

If step 2 fails after step 1 succeeds, the user is shown the draft reference
so they can retry the submission later.

### 3.2 Company review

A company-wide manager (all assignments `AllCompanyBranches` + `payitems.edit`)
sees the **Custom Item Requests** section on the same page. Requests are
filterable by status (Pending / Draft / Approved / Rejected / All). For each
`PendingCompanyApproval` request the reviewer can:

| Action         | Endpoint called |
|----------------|-----------------|
| Approve        | `POST /settings/cdpi/requests/{id}/decide` — `action: "Approve"` |
| Return to Draft| `POST /settings/cdpi/requests/{id}/decide` — `action: "ReturnToDraft"` |
| Reject         | `POST /settings/cdpi/requests/{id}/decide` — `action: "Reject"` |

All three actions require a non-empty `reason` field (enforced client-side
before the call and server-side by the backend). The confirm button is disabled
until `reason.trim()` is non-empty.

### 3.3 Approval creates PayItem

When a request is approved the backend creates the `PayItem` record. The
frontend refreshes the legacy pay-items list (`GET /settings/branches/{id}/pay-items`)
so the new item appears immediately in the left-hand table.

### 3.4 Branch active / inactive

The **Approved Custom Daily Items** section (visible in single-branch mode to
users who pass `canManageCdpiForBranch`) lists all CDPI items returned by
`GET /settings/cdpi/branches/{branch_id}/items`. Each item shows its current
effective `is_active` state with a toggle.

Toggle flow:

1. `PATCH /settings/cdpi/branches/{branch_id}/items/{pay_item_id}`
   with `{ is_active: !currentValue }`
2. Compare returned `is_active` with requested value:
   - Match → toast `"Custom item activated/deactivated for this branch."`
   - No match (change scheduled) → toast
     `"Change saved and scheduled. It will apply after the current protected payroll period."`
3. Trigger list refetch — displayed state always reflects backend's current
   effective value, not an optimistic projection.

### 3.5 Branch display-name override

Inline edit per item in the same section. The user clicks **Edit**, types a
name, and saves.

Save flow:

1. `PATCH /settings/cdpi/branches/{branch_id}/items/{pay_item_id}`
   with `{ branch_display_name_override: value || null }`
   (empty string or clear → `null` to clear the override)
2. Compare returned `branch_display_name_override` with requested value:
   - Match → toast `"Branch display name saved."` or `"Branch display name cleared."`
   - No match → toast
     `"Display-name change saved and scheduled. It will apply after the current protected payroll period."`
3. Trigger list refetch.

---

## 4. Frontend Files Changed

| File | What changed |
|------|-------------|
| `frontend/src/types/settings.ts` | Added all CDPI types: `CdpiRequestSummary`, `CdpiRequestCreatePayload`, `CdpiSubmitPayload`, `CdpiDecidePayload`, `CdpiDirectCreatePayload`, `CdpiDirectCreateSummary`, `CdpiBranchItem`, `CdpiBranchItemUpdatePayload`, `CdpiRequestListParams`, `CdpiDecideAction`, `CdpiStatus` |
| `frontend/src/lib/cdpiApi.ts` | New module — typed wrappers for all six CDPI endpoint groups; no legacy endpoints mixed in |
| `frontend/src/lib/permissions.ts` | Added `hasPayItemsEdit`, `canManageCdpiForBranch`, `canReviewCdpiCompanyWide`, `canDirectCreateCdpiCompanyItem`, `canViewDailyPayItems`; auth-model-gap documented in comments |
| `frontend/src/App.tsx` | Outer `/settings` gate expanded to include `canViewDailyPayItems`; inner `/settings/pay-items` gate changed from `canManageSettingsAdmin` to `canViewDailyPayItems`; `SettingsDefaultRedirect` updated |
| `frontend/src/components/AppShell.tsx` | `payitems.edit`-only users get a settings nav group containing only Daily Pay Items |
| `frontend/src/pages/settings/pay-items/PayItemsPage.tsx` | FE-3 wizard converted; FE-4 CDPI request review section added; FE-5 branch controls section added; all CDPI state follows `useReducer` pattern |
| `frontend/src/pages/settings/pay-items/PayItemsPage.module.css` | Added styles for disabled wizard cards (`.rateMethodCardDisabled`, `.rateMethodBadgeComingSoon`), CDPI request section, CDPI branch controls section |

---

## 5. API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/settings/cdpi/requests` | `GET` | List CDPI requests (filterable by status / branch) |
| `/settings/cdpi/requests` | `POST` | Create draft request |
| `/settings/cdpi/requests/{id}/submit` | `POST` | Submit draft for company approval |
| `/settings/cdpi/requests/{id}/decide` | `POST` | Approve / ReturnToDraft / Reject |
| `/settings/cdpi/direct-company-items` | `POST` | Company-wide direct create (bypasses request workflow) |
| `/settings/cdpi/branches/{branch_id}/items` | `GET` | List CDPI items for a branch |
| `/settings/cdpi/branches/{branch_id}/items/{pay_item_id}` | `PATCH` | Update branch `is_active` / `branch_display_name_override` |

No legacy `/settings/pay-items` mutation endpoints are called by CDPI frontend
code.

---

## 6. Permission Model

### Branch CDPI actions

`canManageCdpiForBranch(user, branchId)` returns `true` in two safe cases:

1. **All assignments `AllCompanyBranches` + `payitems.edit`** — the flat
   `active_permissions` union cannot be contaminated by a SpecificBranch row,
   so `payitems.edit` is provably company-scoped.
2. **Exactly one `SpecificBranch` assignment for the target branch +
   `payitems.edit`** — the single assignment uniquely determines the permission
   source.

All other cases (mixed scope, multiple SpecificBranch rows) return `false`
conservatively. The backend enforces the real boundary.

### Company-wide review / direct create

`canReviewCdpiCompanyWide` / `canDirectCreateCdpiCompanyItem` require that
every branch row is `AllCompanyBranches` AND `active_permissions` includes
`payitems.edit`. Returns `false` when any `SpecificBranch` assignment exists.

### Auth model gap

`BranchAccess` (from `/auth/me`) exposes `scope`, `role_code`, `role_name` but
no per-assignment permission code list. Until the auth model is extended,
multi-branch `SpecificBranch` users with `payitems.edit` will see the helpers
return `false`. This is documented in `permissions.ts`.

### Legacy settings admin

`canManageSettingsAdmin` (`has_setup_manage`) alone does not grant CDPI request
creation or review. CDPI actions require `payitems.edit` with a provably safe
scope, regardless of setup-manage status.

---

## 7. Safety Notes

| Concern | Status |
|---------|--------|
| Old `/settings/pay-items` POST no longer called by wizard | Confirmed — wizard uses `createCdpiRequest` + `submitCdpiRequest` or `createDirectCdpiCompanyItem` |
| No RateTypes / PayItemSettings / PayItemRateTypeMap created by frontend | Confirmed — CDPI flow sends only `item_name`, `input_type`, `calc_method_key`, `unit`, `notes` |
| Non-PerUnit methods disabled | Confirmed — all non-PerUnit `MethodCard` components render with `disabled` prop and "Coming later" badge |
| Pay Rate Names / Short Label not sent | Confirmed — wizard step 3 does not include `rate_names` or `display_label` |
| Branch controls use CDPI endpoints only | Confirmed — `listCdpiBranchItems` / `updateCdpiBranchItem` only; old `/settings/branches/{id}/pay-items` PATCH not used |
| Effective-dated updates | Confirmed — handlers refetch after PATCH and inspect returned state before choosing toast copy; no optimistic projection of future state |
| Legacy pay-item actions (toggle, edit config, bulk, delete, reorder) | Unchanged |

---

## 8. Explicit Exclusions

The following are **not** implemented in FE-1 through FE-5 and remain out of
scope:

- Pay Rate slots
- `RateType` records
- `PayItemRateTypeMap` records
- `PayItemSettings` records
- Payroll entry integration
- Calculation engine
- Finalization
- Ledger behavior
- OrdinalTier, Block, RangeBracket, RangeProgressive calculation methods
  (wizard cards are visible but disabled with "Coming later" badge)

---

## 9. Validation Results

All checks were run at final commit `4574ae0`:

| Check | Result |
|-------|--------|
| `npm run lint` | 0 errors. 1 pre-existing warning in `DashboardPage.tsx` (`react-hooks/exhaustive-deps` on `setWarnings`) — unrelated to CDPI work |
| `npm run build` | Clean build. Pre-existing Vite large-chunk warning (bundle >500 kB) — unrelated |
| `git diff --check` | No whitespace errors. CRLF line-ending warnings only (Windows repo config) |
| `git status` | Clean after each commit |

---

## 10. Recommended Next Phase

**Pay Rates slots / PerUnit rate assignment for CDPI items**

Approved CDPI items currently have no rate configured — they start inactive on
all branches and become visible in payroll entry only after branch activation,
but entering a value generates `$0` until a Pay Rate is attached.

Suggested scope:

- Backend: expose a rate-slot creation endpoint for CDPI `PayItem` records
  (a PerUnit `RateType` + `PayItemRateTypeMap` entry)
- Frontend: after a branch activates a CDPI item, surface a prompt or section
  in Pay Rates to configure the PerUnit rate for that item / branch
- Keep PerUnit as the only enabled method until the architecture for advanced
  methods (OrdinalTier, Block, ranges) is decided

This unblocks drivers from having non-zero pay from CDPI items in payroll entry.
