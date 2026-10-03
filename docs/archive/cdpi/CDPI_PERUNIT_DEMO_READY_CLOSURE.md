# CDPI PerUnit — Closure Report
**Branch:** `custom-daily-refactor` | **Date:** 2026-06-17

---

## 1. Executive Verdict

**CDPI PerUnit is demo-ready.**

All backend milestones are accepted, the frontend is clean, the E2E smoke passed end-to-end for both the Number and Time scenarios, and no P0 or P1 blockers remain open.

---

## 2. What Is Now Working End-to-End

### Company Direct-Create (admin path)
A company admin can create a Custom Daily Pay Item directly from Settings → Pay Items without a request workflow. The item is created at the company level, starts inactive on all branches, and immediately appears in the CDPI Branch Controls panel.

### Branch Request & Approval (branch path)
A branch-level user can submit a CDPI request. The request enters a `PendingCompanyApproval` state visible in the Custom Item Requests panel. A company admin can approve or reject it. On approval the item is automatically activated for the requesting branch.

### Branch Activation Controls
Each branch can independently activate or deactivate any approved CDPI item and set a branch-specific display-name override. Activation state persists and is reflected immediately in Pay Rates and Day Grid visibility for that branch.

### Pay Rates Visibility and Save
Once a CDPI item is activated for a branch, its corresponding rate type (`CDPI_<id>_PER_UNIT`) appears in the rate matrix for drivers on that branch. A rate can be created, submitted, and approved through the standard pay-rates workflow. Inactive CDPI items do not pollute the rate matrix for other branches.

### Current Payroll Day Grid — Visibility and Save
Active CDPI items appear as dedicated columns in the Day Grid alongside standard columns (Hours, Miles). Number items render a numeric input; Time items render a text input with time-format normalization. Inactive CDPI items are absent from the grid.

### Time Input Normalization
The Day Grid time-column parser accepts multiple human formats and normalizes all of them to decimal hours before the payload reaches the backend:

| Input | Normalized |
|-------|-----------|
| `1.5` | `1.5` |
| `1:30` | `1.5` |
| `1:30:00` | `1.5` |
| `1hr 30min` | `1.5` |
| `90m` | `1.5` |
| `1:90` | rejected (reverted on blur) |
| `1:30:99` | rejected (reverted on blur) |
| garbage | rejected (reverted on blur) |

Invalid formats display an inline error and revert the cell; they never reach the save payload.

### Calculation
CDPI PerUnit lines calculate as `quantity × approved driver rate`. Amounts appear immediately as `calculated_amount` in the Day Grid response after save.

### Finalization
Periods containing CDPI draft lines finalize correctly. CDPI amounts are included in `final_gross`. Finalization rejects periods with invalid data through the same guard path as standard lines.

### Final Lines / Ledger Read Visibility
Finalized CDPI lines appear in `GET /payroll/periods/{id}/final-lines` and in the Final Summary dialog (`LineDetailsTable`). Each CDPI line carries the full audit trail: `pay_item_id`, `rate_type_id`, `driver_rate_id`, `resolved_rate_amount`, `rate_behavior=PerUnit`. The resolved rate (not the fallback `rate_amount`) is displayed in the Rate column. A `(PerUnit)` label appears alongside the line type.

### Read-Only Locked-Period Protection
Day Grid save and status-transition endpoints reject requests against Locked periods with a clear 422. The Final Summary dialog is rendered read-only with no edit controls.

---

## 3. Final Smoke Evidence

**Scenario A — Number CDPI item (Smoke Loads)**

| | |
|---|---|
| Item | `Smoke Loads` — Number, PerUnit, $2.50/unit |
| Day Grid entry | 50 loads |
| CDPI line | `50 × $2.50 = $125.00` |
| Standard HOURS line | `8 × $20.00 = $160.00` |
| **Final gross** | **$285.00** ✓ |
| Audit fields | `pay_item_id=15`, `rate_type_id=9`, `driver_rate_id=3`, `resolved_rate_amount=2.5000`, `rate_behavior=PerUnit` |

**Scenario B — Time CDPI item (Smoke Wait Hours)**

| | |
|---|---|
| Item | `Smoke Wait Hours` — Time, PerUnit, $15.00/hr (branch request → approved) |
| Day Grid entry | `1:30` → normalized to `1.5` |
| CDPI line | `1.5 × $15.00 = $22.50` |
| Standard HOURS line | `8 × $20.00 = $160.00` |
| **Final gross** | **$182.50** ✓ |
| Audit fields | `pay_item_id=16`, `rate_type_id=10`, `driver_rate_id=4`, `resolved_rate_amount=15.0000`, `rate_behavior=PerUnit` |

Both periods confirmed Locked, read-only, final lines readable via API and Final Summary dialog.

---

## 4. Bugs Found and Resolved

### CDPI Router Token-Casting P0 (`50e5e93`)

**Symptom:** Every CDPI HTTP endpoint (`/settings/cdpi/...`) returned HTTP 500. Blocked E2E-SMOKE-1 entirely.

**Root cause:** `backend/app/cdpi/router.py` extracted `token["sub"]` without an `int()` cast. Per RFC 7519, `sub` is stored as a string (`"1"`). asyncpg rejected the string when it reached the SQL function `fn_UserHasPermission($1, ...)` which expects `int4`. The payroll router was already handling this correctly with `int(token["sub"])`; the CDPI router was not.

**Fix:** Applied `int(token["sub"])` and `int(token["cid"])` consistently across all 10 CDPI router endpoints.

**Regression tests:** `backend/tests/test_cdpi_http_token.py` — 15 route-level tests covering GET branch items, POST direct-create, POST create-request, GET list (all status filters), unauthenticated denial.

---

### Stale Uvicorn Process on Windows — Pay Items Page 500 After Restart

**Symptom:** Browser showed 500 for CDPI endpoints even after the user reported restarting the backend. The code fix was on disk but the browser-facing server was still serving old code.

**Root cause:** On Windows, uvicorn `--reload` mode spawns a parent reloader process plus a child worker. When the parent is killed (e.g., Ctrl+C or task kill), the child worker and any `multiprocessing.spawn` workers can survive as orphans and continue holding the `127.0.0.1:8000` TCP socket. The Vite dev proxy hardcodes `http://127.0.0.1:8000` as the backend target. New uvicorn processes either fail to bind or bind on `0.0.0.0:8000`; either way, the proxy continues hitting the orphaned pre-fix worker.

**Fix:** Operational — kill all Python processes, then restart (see §6). No code change needed.

**Regression tests:** 6 additional filter-variant tests added to `test_cdpi_http_token.py` (`8a91537`) covering exactly the query patterns the Pay Items page sends on load.

---

## 5. Remaining Non-Blocking Cleanup (P2)

These items do not block demo or sale. They existed before CDPI work began and carry no functional regression.

- **`DashboardPage.tsx:181` lint warning** — `useEffect` missing `setWarnings` dependency. Pre-existing, unrelated to CDPI. One-line fix if desired.
- **Vite bundle size warning** — main JS chunk > 500 KB. Pre-existing. Addressable with code-splitting if the app grows further.
- **Smoke test data in dev database** — periods 2–4, CDPI items 14–16, driver rates 3–4 are present in the development PostgreSQL instance from smoke testing. Safe to leave for inspection; clean up with a direct DB delete before recording a demo if a blank-slate state is preferred.
- **UI polish opportunities** — CDPI wizard copy, request card layout, and branch-controls table are functional but were not redesigned as part of this milestone. Optional future pass.

---

## 6. Operational Note for Windows Dev

If the browser shows CDPI 500 errors immediately after a backend code change and restart, the likely cause is an orphaned pre-fix uvicorn worker still holding `127.0.0.1:8000`. Kill all Python processes and start fresh:

```powershell
Stop-Process -Name python -Force
cd C:\Projects\etbdnt\Payroll_App_v3\backend
python -m uvicorn app.main:app --reload --port 8000
```

Confirm the new server loaded the fix before opening the browser: `curl http://127.0.0.1:8000/docs` should return 200 and the new process should appear in `Get-Process python`.

---

## 7. Recommended Next Step

**Close CDPI PerUnit as demo-ready.** The milestone is complete.

Choose the next phase independently:

| Option | Notes |
|--------|-------|
| **Sales/demo preparation** | Record a clean walkthrough using the smoke scenarios as the script. Clean dev DB first if a blank slate is needed. |
| **UI polish** | Address DashboardPage warning, Vite chunk size, and any CDPI panel cosmetics. Low risk, no schema changes. |
| **Advanced CDPI calc methods** | OrdinalTier, Block, RangeBracket, RangeProgressive. New backend milestone; scope separately. |
| **Legacy lockdown readiness** | Audit remaining non-CDPI payroll paths against the acceptance matrix. |
| **Cleanup pass** | Remove smoke test data, address P2 lint/build warnings, consolidate test fixtures. |
