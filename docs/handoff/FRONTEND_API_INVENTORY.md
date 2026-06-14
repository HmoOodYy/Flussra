# Frontend API Inventory

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

Swagger UI (when backend is running): http://127.0.0.1:8000/docs

All endpoints require a valid JWT unless noted as public.
Send the token as: `Authorization: Bearer <access_token>`

---

## AUTH  —  `/auth`

| Method | Path         | Purpose                          | Permission    | Notes                                           |
|--------|--------------|----------------------------------|---------------|-------------------------------------------------|
| POST   | /auth/login  | Authenticate user, get JWT       | Public        | Body: `{username, password, company_code}`. Returns `{access_token, token_type}`. |
| GET    | /auth/me     | Validate token, get current user | Any valid JWT | Returns user profile, company, scope. Call on app load to verify token. |

**JWT payload fields used by frontend:**
- `sub` — user_id (string)
- `cid` — company_id (string)
- `exp` — expiry (Unix timestamp)

**Auth flow:**
1. POST /auth/login → receive `access_token`
2. Store token (memory or sessionStorage — avoid localStorage for security)
3. Add `Authorization: Bearer <token>` header to every subsequent request
4. On 401 response → clear token, redirect to login
5. Call GET /auth/me on app load to verify token is still valid

---

## CORE  —  `/core`

| Method | Path                    | Purpose                        | Permission      | Notes                                        |
|--------|-------------------------|--------------------------------|-----------------|----------------------------------------------|
| GET    | /core/branches          | List company branches          | Any valid JWT   | Returns all branches the user can access.    |
| GET    | /core/branches/{id}     | Get a single branch            | Any valid JWT   | Includes branch metadata.                    |
| GET    | /core/people            | List people (drivers + non)    | Any valid JWT   | Scoped to user's accessible branches.        |
| GET    | /core/drivers           | List drivers                   | Any valid JWT   | Filter by `branch_id`. Active drivers only by default. |
| POST   | /core/drivers           | Create driver                  | `drivers.manage`| Creates a person + driver record.            |
| PATCH  | /core/drivers/{id}      | Update driver                  | `drivers.manage`| Update name, status, etc.                    |

**Branch scope for frontend:**
- `AllCompanyBranches` users: show branch picker, pass `branch_id` query param to filter.
- `SpecificBranch` users: branch is fixed — skip picker, use assigned branch_id directly.
- GET /auth/me returns `scope_type` and `branch_ids` list.

---

## PAYROLL  —  `/payroll`

### Periods

| Method | Path                                  | Purpose                           | Permission         | Notes                                            |
|--------|---------------------------------------|-----------------------------------|--------------------|--------------------------------------------------|
| GET    | /payroll/periods                      | List payroll periods              | `payroll.entry`    | Filter: `branch_id`, `status`. Paginated.        |
| POST   | /payroll/periods                      | Create a new period (Draft)       | `payroll.entry`    | Body: `{branch_id, period_type, start_date, end_date}`. |
| PATCH  | /payroll/periods/{id}/status          | Advance or cancel period status   | varies by transition | See status transition table below.             |

**Period status transitions and required permissions:**

| From       | To          | Permission           | Notes                                           |
|------------|-------------|----------------------|-------------------------------------------------|
| Draft      | Open        | `payroll.entry`      | Enables draft line entry.                       |
| Open       | InReview    | `payroll.entry`      | Auto-creates PeriodApproval review item. Atomic. |
| Open       | Cancelled   | `payroll.finalize`   |                                                 |
| InReview   | Open        | `payroll.finalize`   | Manual pull-back.                               |
| InReview   | Cancelled   | `payroll.finalize`   |                                                 |
| Approved   | InReview    | `payroll.finalize`   | Re-open for corrections.                        |
| Approved   | Cancelled   | `payroll.finalize`   |                                                 |
| Locked     | Archived    | `payroll.finalize`   | After finalization.                             |

Note: InReview→Approved is driven by review decision, not direct PATCH.
Note: Approved→Locked is driven by POST .../finalize.

**Period status meanings for UI:**

| Status    | Color suggestion | Meaning                                          |
|-----------|------------------|--------------------------------------------------|
| Draft     | Gray             | Created, not yet open for entry.                 |
| Open      | Blue             | Active for daily/period-pay entry.               |
| InReview  | Amber            | Submitted for manager approval.                  |
| Approved  | Green (outline)  | Manager approved, ready to finalize.             |
| Locked    | Green (solid)    | Finalized and locked. Immutable.                 |
| Archived  | Purple           | Archived after finalization.                     |
| Cancelled | Red              | Cancelled. No further action.                    |

### Daily Draft Lines

| Method | Path                                        | Purpose                        | Permission      | Notes                                         |
|--------|---------------------------------------------|--------------------------------|-----------------|-----------------------------------------------|
| GET    | /payroll/periods/{id}/lines                 | List draft lines for period    | `payroll.entry` | Returns per-line records.                     |
| POST   | /payroll/periods/{id}/lines                 | Add a daily draft line         | `payroll.entry` | Body: `{driver_id, line_type, quantity, rate_amount, work_date, source_type}`. |
| PATCH  | /payroll/periods/{id}/lines/{line_id}       | Update a daily draft line      | `payroll.entry` | Partial update: quantity, rate_amount, work_date. |
| DELETE | /payroll/periods/{id}/lines/{line_id}       | Void a daily draft line        | `payroll.entry` | Soft-void (sets voided flag). Period must be Open or InReview. |
| GET    | /payroll/periods/{id}/lines/summary         | Per-driver totals for period   | `payroll.entry` | Shows subtotals per driver per line type.     |

**Line type values:** Use `pay_item_code` from GET /settings/branches/{id}/pay-items
(active items only). System codes include: `Miles`, `Hours`, `Loads`, `Stops`, `Pieces`,
`FlatRate`, `Overnight`, `Bonus`, `Adjustment`. Custom codes are company-specific.

**needs_manager_review flag:** If a line has `needs_manager_review=true` (e.g. no
approved rate exists for the driver), the period cannot be submitted or finalized until
resolved. Show a warning badge on such lines.

### Period Pay Lines

| Method | Path                                           | Purpose                           | Permission      | Notes                                          |
|--------|------------------------------------------------|-----------------------------------|-----------------|------------------------------------------------|
| GET    | /payroll/periods/{id}/period-pay               | List period-pay lines             | `payroll.entry` | Period-scoped payments (Bonus, Adjustment, etc.). |
| POST   | /payroll/periods/{id}/period-pay               | Add a period-pay line             | `payroll.entry` | Body: `{driver_id, line_type, amount}`. line_type must be Period-scope (EnteredAmount). |
| PATCH  | /payroll/periods/{id}/period-pay/{line_id}     | Update period-pay amount          | `payroll.entry` | Body: `{amount}`.                              |
| DELETE | /payroll/periods/{id}/period-pay/{line_id}     | Void a period-pay line            | `payroll.entry` | Soft-void.                                     |

### Finalization

| Method | Path                                 | Purpose                           | Permission          | Notes                                           |
|--------|--------------------------------------|-----------------------------------|---------------------|-------------------------------------------------|
| POST   | /payroll/periods/{id}/finalize        | Finalize approved period → Locked | `payroll.finalize`  | Creates immutable final lines. Period must be Approved. |
| GET    | /payroll/periods/{id}/final-lines     | View finalized lines              | `payroll.entry`     | Immutable ledger. Includes system top-up/cap lines. |

### Pay Rates

| Method | Path                           | Purpose                                  | Permission              | Notes                                         |
|--------|--------------------------------|------------------------------------------|-------------------------|-----------------------------------------------|
| GET    | /payroll/rates                 | List driver rates                        | `payroll.entry`         | Filter: `driver_id`, `rate_type_id`, `status`. |
| POST   | /payroll/rates                 | Create a pending rate                    | `payroll.entry`         | Body: `{driver_id, rate_type_id, rate_value, effective_from, ...}`. |
| PATCH  | /payroll/rates/{id}            | Update a pending rate                    | `payroll.entry`         | Only PendingApproval rates.                   |
| DELETE | /payroll/rates/{id}            | Void a rate                              | `payroll.entry`         | Soft-void. Cannot void if Locked/Archived period governed it. |
| POST   | /payroll/rates/{id}/approve    | Approve a pending rate                   | `payroll.approve_rate`  | Atomic. Supersedes any prior approved rate for same driver+type. |
| GET    | /payroll/rates/lookup          | Look up effective rate for driver+date   | `payroll.entry`         | Params: `driver_id`, `rate_type_id`, `effective_date`. |

**Rate statuses:** PendingApproval → Approved (or Voided/Superseded).

### Driver Pay Rules

| Method | Path                                       | Purpose                          | Permission      | Notes                                              |
|--------|--------------------------------------------|----------------------------------|-----------------|----------------------------------------------------|
| GET    | /payroll/driver-pay-rules                  | List pay rules for drivers       | `payroll.entry` | Filter: `driver_id`.                               |
| POST   | /payroll/driver-pay-rules                  | Create a MinimumPay or MaximumPay rule | `payroll.entry` | Body: `{driver_id, rule_type, amount, effective_from}`. |
| POST   | /payroll/driver-pay-rules/{id}/void        | Void a rule                      | `payroll.entry` | Blocked if Locked/Archived period was governed by it. |
| POST   | /payroll/driver-pay-rules/{id}/end         | Set an end date on a rule        | `payroll.entry` | Blocked if end date falls before a governed Locked/Archived period. |

---

## REVIEW  —  `/review`

| Method | Path                        | Purpose                               | Permission      | Notes                                              |
|--------|-----------------------------|---------------------------------------|-----------------|----------------------------------------------------|
| GET    | /review/items               | List review items                     | Any valid JWT   | Filter: `status`, `branch_id`, `request_type`. Scoped to accessible branches. |
| POST   | /review/items               | Create a manual review item           | `payroll.entry` | For Correction, Dispute, etc. **NOT PeriodApproval** — that is auto-created on InReview. |
| POST   | /review/items/{id}/decide   | Approve / Reject / EditRequested / Comment | `review.decide` | Body: `{decision, reason}`. PeriodApproval decisions update the linked period status. |

**Review item types:**
- `PeriodApproval` — auto-created when period moves to InReview. Decision drives period status.
- `Correction`, `Dispute`, etc. — manual items. Decision only updates the review item itself.

**PeriodApproval decision effects:**
| Decision      | Period status change       |
|---------------|---------------------------|
| Approved      | InReview → Approved       |
| Rejected      | InReview → Open           |
| EditRequested | InReview → Open           |
| Comment       | No period change           |

---

## SETTINGS  —  `/settings`

### Company

| Method | Path              | Purpose               | Permission     | Notes                              |
|--------|-------------------|-----------------------|----------------|------------------------------------|
| GET    | /settings/company | Get company profile   | Any valid JWT  |                                    |
| PATCH  | /settings/company | Update company profile| `setup.manage` | Name, timezone, AllowSelfApproval. |

### Branches

| Method | Path                                  | Purpose                     | Permission     | Notes                             |
|--------|---------------------------------------|-----------------------------|----------------|-----------------------------------|
| GET    | /settings/branches                    | List all branches           | Any valid JWT  |                                   |
| POST   | /settings/branches                    | Create a branch             | `setup.manage` |                                   |
| PATCH  | /settings/branches/{id}               | Update a branch             | `setup.manage` |                                   |
| POST   | /settings/branches/{id}/set-default   | Set as default branch       | `setup.manage` |                                   |

### Branch Payroll Setup

| Method | Path                                       | Purpose                         | Permission     | Notes                               |
|--------|--------------------------------------------|---------------------------------|----------------|-------------------------------------|
| GET    | /settings/branches/{id}/payroll-setup      | Get payroll setup for branch    | Any valid JWT  | Returns rate types, week start, etc.|
| PUT    | /settings/branches/{id}/payroll-setup      | Save payroll setup for branch   | `setup.manage` | Full replace.                       |

### Status Keys

| Method | Path                                          | Purpose                     | Permission     | Notes                        |
|--------|-----------------------------------------------|-----------------------------|----------------|------------------------------|
| GET    | /settings/branches/{id}/status-keys           | List status keys            | Any valid JWT  | Driver employment status codes. |
| POST   | /settings/branches/{id}/status-keys           | Create a status key         | `setup.manage` |                              |
| PATCH  | /settings/branches/{id}/status-keys/{key_id}  | Update a status key         | `setup.manage` |                              |
| DELETE | /settings/branches/{id}/status-keys/{key_id}  | Delete a status key         | `setup.manage` |                              |

### Pay Items (Branch Config)

| Method | Path                                                    | Purpose                              | Permission     | Notes                                            |
|--------|---------------------------------------------------------|--------------------------------------|----------------|--------------------------------------------------|
| GET    | /settings/branches/{id}/pay-items                       | List pay items with branch config    | Any valid JWT  | Shows `is_active`, `is_using_default`, `item_scope`. |
| GET    | /settings/branches/{id}/pay-items/{item_id}             | Get one pay item with branch config  | Any valid JWT  |                                                  |
| PATCH  | /settings/branches/{id}/pay-items/{item_id}             | Activate/deactivate for branch       | `setup.manage` | Body: `{is_active, effective_from?}`. Open-period protected. |
| GET    | /settings/branches/{id}/pay-items/{item_id}/history     | Config version history              | Any valid JWT  |                                                  |
| GET    | /settings/branches/{id}/pay-items/missing               | Pay items not yet configured        | Any valid JWT  | Items available company-wide but not active here. |

**Pay item fields for daily line entry:**
- `pay_item_code` — use as `line_type` when adding lines
- `item_scope` — `Daily` (qty × rate) or `Period` (entered dollar amount)
- `rate_behavior` — `PerUnit`, `EnteredAmount`, `OrdinalTier`, `RangeBracket`, etc.
- `is_active` — only show active items in entry dropdowns

### Custom Pay Items (Admin)

| Method | Path                                    | Purpose                           | Permission     | Notes                                            |
|--------|-----------------------------------------|-----------------------------------|----------------|--------------------------------------------------|
| GET    | /settings/pay-items                     | List company custom pay items     | `setup.manage` | Excludes Retired by default. Pass `include_retired=true` for full catalog. |
| POST   | /settings/pay-items                     | Create custom pay item (admin)    | `setup.manage` | Admin direct path — no request/approval flow.    |
| GET    | /settings/pay-items/{item_id}           | Get one custom pay item           | `setup.manage` |                                                  |
| PATCH  | /settings/pay-items/{item_id}           | Update a custom pay item          | `setup.manage` |                                                  |
| DELETE | /settings/pay-items/{item_id}           | Delete or retire a pay item       | `setup.manage` | Smart delete: physical if unused, Retired if used. |
| GET    | /settings/pay-items/{item_id}/usage     | Check usage before delete         | `setup.manage` | Returns `deletion_would_retire` flag.            |
| POST   | /settings/pay-items/{item_id}/rate-type-map | Set rate type mapping         | `setup.manage` |                                                  |

### Pay Item Requests (Branch Flow)

| Method | Path                                      | Purpose                                  | Permission     | Notes                                         |
|--------|-------------------------------------------|------------------------------------------|----------------|-----------------------------------------------|
| GET    | /settings/pay-item-requests               | List pay item requests                   | Any valid JWT  | Filter by status, branch.                     |
| POST   | /settings/pay-item-requests               | Submit a request for a new pay item      | `payroll.entry`| Branch requests a new pay item code.          |
| GET    | /settings/pay-item-requests/{id}          | Get one request                          | Any valid JWT  |                                               |
| POST   | /settings/pay-item-requests/{id}/decide   | Approve or reject a request              | `setup.manage` | Approval creates the PayItem + BranchPayItemConfig. |

---

## ADMIN  —  `/admin`

| Method | Path                              | Purpose                        | Permission     | Notes                                              |
|--------|-----------------------------------|--------------------------------|----------------|----------------------------------------------------|
| GET    | /admin/users                      | List users for company         | `setup.manage` | AllCompanyBranches scope required.                 |
| POST   | /admin/users                      | Create a user                  | `setup.manage` |                                                    |
| PATCH  | /admin/users/{id}                 | Update a user                  | `setup.manage` |                                                    |
| POST   | /admin/users/{id}/reset-password  | Reset user password            | `setup.manage` |                                                    |
| GET    | /admin/users/{id}/roles           | List roles for user            | `setup.manage` |                                                    |
| POST   | /admin/users/{id}/roles           | Assign a role to user          | `setup.manage` | Body: `{role_id, branch_id?, scope_type}`.         |
| DELETE | /admin/users/{id}/roles/{ubr_id}  | Remove a role from user        | `setup.manage` |                                                    |
| GET    | /admin/roles                      | List available roles           | `setup.manage` |                                                    |
| GET    | /admin/permissions                | List available permissions     | `setup.manage` |                                                    |

**Scope types for role assignment:**
- `AllCompanyBranches` — user sees all branches; `branch_id` must be NULL.
- `SpecificBranch` — user sees only this branch; `branch_id` required.

---

## DASHBOARD  —  `/dashboard`

| Method | Path        | Purpose                          | Permission      | Notes                                            |
|--------|-------------|----------------------------------|-----------------|--------------------------------------------------|
| GET    | /dashboard  | Get home screen summary          | `payroll.entry` | Per-branch permission for SpecificBranch users.  |

**Response shape:**
```json
{
  "generated_at": "2026-05-30T12:00:00Z",
  "scope": "AllCompanyBranches",
  "periods_draft": 2,
  "periods_open": 1,
  "periods_in_review": 0,
  "periods_approved": 0,
  "periods_locked": 14,
  "review_pending": 0,
  "review_edit_requested": 0,
  "active_drivers": 8,
  "last_finalized_period": {
    "period_id": 42,
    "period_name": "Week 2026-05-18",
    "branch_id": 1,
    "branch_name": "Headquarters",
    "start_date": "2026-05-18",
    "end_date": "2026-05-24",
    "locked_at": "2026-05-25T10:00:00Z"
  },
  "setup_warnings": [
    {
      "code": "BRANCH_NO_PAYROLL_SETTINGS",
      "severity": "Warning",
      "message": "Branch has no payroll settings configured.",
      "branch_id": 2,
      "branch_name": "PAYTEST",
      "count": null
    }
  ],
  "branch_summaries": [
    {
      "branch_id": 1,
      "branch_name": "Headquarters",
      "draft_count": 1,
      "open_count": 1,
      "in_review_count": 0,
      "approved_count": 0,
      "locked_count": 14,
      "pending_review_count": 0,
      "edit_requested_count": 0,
      "active_driver_count": 6,
      "needs_manager_review_lines": 0
    }
  ]
}
```

**Setup warning codes:**
| Code                           | Meaning                                                  |
|--------------------------------|----------------------------------------------------------|
| `BRANCH_NO_PAYROLL_SETTINGS`   | Branch has no payroll setup configured.                  |
| `OPEN_PERIOD_NEEDS_MANAGER_REVIEW` | Open period has lines flagged needs_manager_review.  |
| `DRIVERS_NO_APPROVED_RATE`     | Active drivers have no approved rate of any type.        |
| `PAY_ITEM_MISSING_RATE_TYPE_MAP` | A PerUnit pay item has no rate type mapping.           |

---

## Common Error Shapes

All error responses follow FastAPI's default:

```json
{ "detail": "Human-readable message here" }
```

| HTTP Status | Meaning                                             |
|-------------|-----------------------------------------------------|
| 401         | No token or invalid/expired token                   |
| 403         | Valid token but insufficient permission or scope    |
| 404         | Resource not found (or not accessible to this user) |
| 422         | Validation error or business rule violation         |
| 500         | Unexpected server error (should be rare)            |

---

## JWT Token Notes

- Tokens expire. On 401, redirect to login.
- `exp` field in JWT payload is the Unix expiry timestamp.
- Token contains: `sub` (user_id), `cid` (company_id), `scope_type`, `branch_ids`.
- Frontend should NOT decode and trust JWT claims for authorization decisions —
  the backend re-validates on every request. Use claims only for UI hints
  (e.g., showing branch picker vs. fixed branch).
