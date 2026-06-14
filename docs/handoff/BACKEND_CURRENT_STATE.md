# Backend Current State

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

## Folder Structure

```text
C:\Projects\etbdnt\Payroll_App_v3
  alembic.ini
  backend
    .env
    .env.example
    pyproject.toml
    app
      main.py
      config.py
      dependencies.py
      db
        session.py
      auth
      core
      payroll
      review
      settings
      admin
      rates
      dashboard
    tests
  docs
    handoff
  frontend
  migrations
    env.py
    sql
    versions
```

Notes:

- `backend\app\rates` contains only package scaffolding; rate endpoints live under `backend\app\payroll`.
- `frontend` exists but no frontend implementation has been written yet. Frontend is safe to start.

## Routers

Registered in `backend\app\main.py`:

| Prefix       | Router file                             |
|--------------|------------------------------------------|
| `/auth`      | `backend\app\auth\router.py`            |
| `/core`      | `backend\app\core\router.py`            |
| `/payroll`   | `backend\app\payroll\router.py`         |
| `/review`    | `backend\app\review\router.py`          |
| `/settings`  | `backend\app\settings\router.py`        |
| `/admin`     | `backend\app\admin\router.py`           |
| `/dashboard` | `backend\app\dashboard\router.py`       |

## Services

- `backend\app\auth\service.py`
- `backend\app\core\service.py`
- `backend\app\payroll\service.py`
- `backend\app\review\service.py`
- `backend\app\settings\service.py`
- `backend\app\admin\service.py`
- `backend\app\dashboard\service.py`

## Migrations

| File                                            | Description                                       |
|-------------------------------------------------|---------------------------------------------------|
| `migrations\versions\0001_initial_schema.py`   | Full initial schema                               |
| `migrations\versions\0002_safety_indexes.py`   | Safety indexes                                    |
| `migrations\versions\0003_pay_items_seed.py`   | 11 system pay items seeded                        |
| `migrations\versions\0004_review_policy.py`    | AllowSelfApproval on core.Companies               |
| `migrations\versions\0005_custom_pay_items.py` | Custom pay items schema                           |
| `migrations\versions\0006_request_active_code_index.py` | Partial unique index for pending requests |
| `migrations\versions\0007_payitemratetypemap_system_seed.py` | PayItemRateTypeMap seed (6 system PerUnit items) |
| `migrations\versions\0008_ratetypes_seed.py`   | 7 RateTypes seeded; idempotent map re-seed        |
| `migrations\versions\0009_tier_rate_structures.py` | DriverRateTiers; BlockSize/RoundingRule on DriverRates; expanded CHECK |
| `migrations\versions\0010_period_pay_index.py` | LineScope on PayrollDraftLines + partial index    |
| `migrations\versions\0011_final_lines_linescope.py` | LineScope on PayrollFinalLines                |
| `migrations\versions\0012_driver_pay_rules.py` | DriverPayRules table; SYS_MIN_TOPUP, SYS_MAX_CAP |
| `migrations\versions\0013_period_approval_index.py` | Partial unique index: one Pending PeriodApproval per period |

**Current head: `0013`**

Key migration notes:
- 0010 adds LineScope (VARCHAR(10), DEFAULT 'Daily', CHECK IN ('Daily','Period')) to
  PayrollDraftLines and a partial index WHERE LineScope='Period'.
- 0011 adds the same to PayrollFinalLines (immutable ledger preserves Daily/Period scope).
- 0012 adds payroll.DriverPayRules table and seeds SYS_MIN_TOPUP, SYS_MAX_CAP system items.
- 0013 adds partial unique index ux_ReviewItems_OnePendingPeriodApproval on
  review.ManagerReviewItems: at most one Pending PeriodApproval per (companyid, entityid).
- M16 required no new migration.
- M17 required no new migration.

## How To Run The Server

From `C:\Projects\etbdnt\Payroll_App_v3\backend`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Swagger UI: http://127.0.0.1:8000/docs

## How To Run Tests

From `C:\Projects\etbdnt\Payroll_App_v3\backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

**Latest verified result: `734 passed, 1 skipped, 0 failed`**

The 1 skipped test (`TestDashboardSetupWarnings::test_no_warnings_when_all_clear`) is
environment-conditional and not a coverage gap.

## How To Run Migrations

From `C:\Projects\etbdnt\Payroll_App_v3`:

```powershell
# Upgrade to latest
python -m alembic upgrade head

# Check current head
python -m alembic current

# Downgrade one step
python -m alembic downgrade -1
```

Verify Alembic current = 0013 after upgrade.

## Local Database Note

`alembic current` against `backend\.env` may fail with invalid `payroll_user` password.
Fix credentials in `.env` before using the local development database.

The dev database has been manually upgraded to 0013 by the project owner.

## Key Security and Concurrency Properties

- Open→InReview: atomic `UPDATE WHERE status='Open' RETURNING` prevents double-submit.
- Concurrent PeriodApproval race: `SAIntegrityError` on unique index caught → clean 422.
- DB partial unique index `ux_ReviewItems_OnePendingPeriodApproval` is the final safety net.
- Manual PeriodApproval creation via POST /review/items returns 422 unconditionally.
- decide_review_item validates entity_schema/entity_name, period existence, company, branch.
- Admin `_ensure_admin` revalidates user and company status on every sensitive admin call.
- Dashboard payroll.entry permission checked per-branch for SpecificBranch users.
- Draft-line and period-pay mutations write audit rows in the same transaction; failure
  rolls back the primary write.
- DriverPayRules: Archived periods are treated as finalized (same as Locked) in all
  period-range protection checks.
