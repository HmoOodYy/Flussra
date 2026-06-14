# Architecture Decisions

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

## Python And FastAPI Backend

Decision: The backend is Python with FastAPI.

Reason: FastAPI fits an API-first backend, typed request/response schemas,
async database access, and automated OpenAPI generation.

Consequence: Future frontend work should consume the FastAPI API contract.
Backend validation remains in Pydantic schemas and service-layer business rules.

---

## PostgreSQL Source Of Truth

Decision: PostgreSQL is the authoritative data store.

Reason: Payroll requires transactional safety, constraints, auditability,
and durable historical records.

Consequence:
- Business-critical integrity is enforced in both service code and DB constraints.
- Tests use real PostgreSQL (via testing.postgresql), not SQLite.

---

## React and TypeScript Frontend

Decision: The frontend is React and TypeScript.

Status: Backend complete. Frontend is safe to start as of M18 (2026-05-30).

Reason: React/TypeScript can consume the REST API cleanly and supports the
component-based payroll entry UI the project requires.

Consequence:
- All frontend data access must go through the FastAPI backend.
- Keep API response shapes stable or version changes carefully once the
  frontend begins consuming them.

---

## API-First Modular Monolith

Decision: The backend is a modular monolith with routers, services, schemas,
and database access in one deployable API.

Reason: Keeps payroll workflow transactional and easier to reason about while
still separating domains cleanly.

Consequence: New features follow the existing module pattern:
- `router.py` handles HTTP shape (path, method, status code, request/response models).
- `schemas.py` handles Pydantic validation.
- `service.py` handles authorization, branch scope, business rules, SQL, audit, rollback.

---

## Old PySide Project Is Reference-Only

Decision: `C:\Projects\etbdnt\Payroll_App_original_uploaded_backup` is reference-only.

Reason: The new system is being rebuilt on a backend API foundation.

Consequence: Do not modify old PySide files. Use only to understand legacy
business workflow or terminology.

---

## No Direct Database Access From UI

Decision: UI must not connect directly to PostgreSQL.

Reason: Access control, audit logging, validation, and transaction safety live
in the backend.

Consequence: Every frontend workflow must use API endpoints. No frontend
shortcuts that bypass FastAPI services.

---

## Effective-Dated Rates And Pay Items

Decision: Driver rates and branch pay item configs are effective-dated.

Reason: Payroll must preserve historical behavior. Current or finalized payroll
must not change unexpectedly when a new rate or pay item config starts later.

Consequence:
- New rate or config changes create future versions or supersede previous ones.
- Approved rates carry the rate value that was approved at approval time.
- Historical approved rates are never overwritten.

---

## Immutable Finalized Payroll

Decision: Finalized payroll lines are immutable.

Reason: Payroll needs a locked ledger that cannot be silently changed by later
rates, pay item config edits, or draft line edits.

Consequence:
- Draft lines are blocked after the period is Locked.
- Final lines are not exposed through mutation APIs.
- Future correction workflows must be additive and audited, not direct edits.

---

## Audit-First Design

Decision: Sensitive writes write audit rows in the same transaction as the
primary change.

Reason: Payroll and admin actions need traceability. If audit fails, the
primary write rolls back.

Consequence:
- New write features must include audit entries and rollback tests.
- `_write_line_audit` in payroll/service.py is module-level so it is
  monkeypatchable in tests for rollback verification.

---

## Branch Scope And Action-Level Permissions

Decision: Access is controlled by both branch/company scope and action-level
permissions enforced in every service.

Reason: Users may have access to only some branches and may be read-only
within those branches.

Scope pattern:
- `AllCompanyBranches` — user sees all branches for the company.
- `SpecificBranch` — user sees only explicitly assigned branches.

Permission codes:
| Code                 | Purpose                                     |
|----------------------|---------------------------------------------|
| `payroll.entry`      | Create/edit draft lines, submit for review  |
| `payroll.finalize`   | Lock periods, archive, cancel open periods  |
| `payroll.approve_rate` | Approve pending driver rates              |
| `review.decide`      | Approve/reject/comment on review items      |
| `setup.manage`       | Manage company settings, users, pay items   |
| `drivers.manage`     | Create and edit drivers                     |

Consequence:
- Every endpoint enforces branch/company scope.
- Every sensitive write requires the relevant permission code.
- Dashboard payroll.entry is checked per-branch for SpecificBranch users.

---

## Alembic Migration Discipline

Decision: Schema changes use Alembic migrations backed by raw SQL files.

Pattern:
- `migrations/sql/NNNN_name.sql` — the SQL change (plain ASCII only on Windows).
- `migrations/versions/NNNN_name.py` — the Alembic wrapper.

Reason: PostgreSQL schema is complex and includes constraints, indexes, functions,
and views that need exact SQL control.

Consequence:
- Do not edit old applied migrations.
- Add a new migration file for every schema change.
- After any migration edit, verify fresh `alembic upgrade head` from 0001.
- The migration SQL files must use only ASCII characters (psycopg2 WIN1252
  encoding constraint on Windows — no Unicode arrows, curly quotes, etc.).
- Current head: `0013`.

---

## Concurrency and Data Integrity

Decision: The Open→InReview transition uses defense in depth for concurrent
duplicate-submit protection.

Layers (innermost wins):
1. Service pre-flight: SELECT for existing Pending PeriodApproval.
2. Atomic UPDATE: `UPDATE WHERE status='Open' RETURNING` — only one request wins.
3. DB unique index: `ux_ReviewItems_OnePendingPeriodApproval` on
   `review.ManagerReviewItems` — DB-level final guard.
4. SAIntegrityError catch: converts unique-index violation into clean HTTP 422.

Reason: Sequential double-submit returns 422 via (2). True concurrent race
is caught by (3)+(4). Raw DB errors never reach the client.

Consequence: Any future race-sensitive INSERT should follow the same pattern:
unique index + SAIntegrityError catch → clean 422.
