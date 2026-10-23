# Flussra

Payroll management application — FastAPI + PostgreSQL backend with a React + TypeScript frontend.

## Stack

- **Backend**: FastAPI, SQLAlchemy (async), PostgreSQL, Alembic migrations, JWT auth
- **Frontend**: React 19, TypeScript, Vite, React Router

## Project structure

```
backend/        FastAPI app (app/), tests, pyproject.toml
frontend/       React app (src/), tests
migrations/     Alembic migration scripts
docs/           Architecture and status notes
scripts/        Dev/ops helper scripts
```

## Backend setup (Windows PowerShell)

```powershell
.\scripts\setup_backend.ps1
.\scripts\test_backend.ps1
.\scripts\dev_backend.ps1
```

Each Git worktree uses its own ignored `backend/.venv`. Setup installs the
editable backend and `[dev]` dependencies from `backend/pyproject.toml`. The
scripts do not create or copy `.env`; configure local environment values
manually when needed.

## Development database reset and canonical demo

Development data is disposable and is never converted. To reset the database
configured in `backend/.env` (requires `ENVIRONMENT=development`), repeat its
name from the `backend` directory:

```powershell
.\.venv\Scripts\python.exe scripts\reset_dev_database.py --confirm payroll_v3
```

The reset drops every schema and extension the database role owns, runs
`alembic upgrade head`, asserts the single Alembic head, runs the startup schema
guard, writes the canonical demo (`scripts/canonical_demo.py`) and verifies it.
It keeps the database itself, so a database owner without `CREATEDB` can run it.
`--verify-only` changes nothing and checks that the database is still the
canonical demo.

The canonical demo is one payroll-ready Company, `DEMO` / Demo Logistics in
`USD`, with no payroll history:

- Branches `HQ` (default, America/New_York) and `NORTH` (America/Chicago).
- `admin`: Company Owner over all Branches, not linked to an Employee.
- `payroll.manager`: custom Payroll Manager role scoped to `HQ` (payroll entry,
  period creation, review, finalization, reports and the Finalized Payroll
  Library, plus Driver and rate reads).
- Morgan Lee: staff Employee without a Driver or account.
- Jordan Rivera (`D-1001`, `HQ`): current Driver with the DRIVER/Self account
  `jordan.rivera` and an Approved `DELIVERY` rate of 12.50 per stop.
- Casey Brooks (`D-1002`, `NORTH`): future Driver whose assignment starts 14
  days after the reset; the linked account `casey.brooks` stays staged (no login,
  no role) until it is provisioned.
- PayDefinition `DELIVERY` (PerUnit, whole stops) applicable to `HQ`, and the
  default weekly Payroll Setup assigned to `HQ` from the Monday of the week
  before the reset, so the first `HQ` period can be created at once.

Every demo account uses the password `TestPass123!`.

## Frontend setup

```bash
cd frontend
npm install
npm run dev
```

Build / lint:

```bash
npm run build
npm run lint
```

## Canonical local validation

Run the repository's complete local validation contract from the repository
root in PowerShell:

```powershell
.\scripts\validate.ps1
```

The command runs the full backend pytest suite, Ruff, Python compileall, and an
assertion that Alembic has exactly one head at `0093`. It then runs every
frontend `tests/*.test.ts` file, ESLint, and the production build. A successful
exit means every required gate passed; the command stops and returns nonzero
when a gate fails or required tooling is missing. Install the worktree-local
backend environment with `.\scripts\setup_backend.ps1` and frontend
dependencies with `npm ci` from `frontend/` before validation.

During validation, the script verifies that `C:\Temp` is writable and sets
`TEMP` and `TMP` to that directory for its process and child tools only. It
restores the caller's original values and removes its invocation-owned
compileall cache directory when validation ends.

GitHub Actions runs this canonical validation on pull requests to `main` and
pushes to `main`; the schema authority equivalence command runs as an independent
check. Both use the repository-owned local authorities.

## Database schema authority comparison

The temporal authority and cutover contract is documented in
[`docs/architecture/BRANCH_TIMEZONE_AUTHORITY.md`](docs/architecture/BRANCH_TIMEZONE_AUTHORITY.md).

To compare the direct SQL bootstrap with a fresh Alembic upgrade through the
current head, run this from the repository root after backend setup:

```powershell
.\scripts\validate_schema_authorities.ps1
```

The command creates two invocation-owned disposable PostgreSQL databases,
compares their application catalogs, and removes only those databases and its
temporary PostgreSQL cluster when it exits.

## Branching

- `main` — canonical, always-deployable branch
- Short-lived feature branches for implementation work, merged via pull request
- Tags mark historical/release milestones (e.g. `pre-refactor-baseline`)
