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

Development data is disposable and is never converted. A database can be reset
only after it has been explicitly provisioned as a resettable Flussra development
database. The trust decision is the database's own identity, never its network
location: a remote, Docker, WSL or cloud development database can be provisioned,
and a local database that is not provisioned cannot be reset.

Provision once, as a PostgreSQL administrator (a superuser, or a role granted
`SET` on the `flussra.*` parameters), from the `backend` directory:

```powershell
.\.venv\Scripts\python.exe scripts\provision_dev_database.py --admin-url postgresql://postgres@localhost:5432/payroll_v3 --confirm payroll_v3
```

This stores two database-level settings in the PostgreSQL catalog:
`flussra.environment = development` (what the database is) and
`flussra.reset_allowed = on` (destructive reset is allowed). They survive every
reset. The application role cannot store them for its own database, and the
reset command never grants them. A database already marked as another
environment is refused. To withdraw the capability, an administrator runs
`ALTER DATABASE payroll_v3 SET flussra.reset_allowed = off`.

Then reset the database configured in `backend/.env` by repeating its name:

```powershell
.\.venv\Scripts\python.exe scripts\reset_dev_database.py --confirm payroll_v3
```

The reset runs only when all of these hold:

- `ENVIRONMENT=development` is set explicitly in the environment or `.env` (the
  application default does not count);
- the connected database's `flussra.environment` is `development`;
- its `flussra.reset_allowed` is `on`;
- `--confirm` names the database actually connected to.

A missing or different value refuses the reset.

The destructive step runs in an exclusive window. The reset closes the database
to new connections (`ALLOW_CONNECTIONS false`) from a short control session in
another database on the same server, by default `postgres` (change it with
`--control-database`), because PostgreSQL does not let a session close its own
database. It then checks that no other session is connected. Stop the backend
and any other client first: an open session refuses the reset, and the reset
never terminates sessions. A second reset of the same database is refused while
one is running. The database is reopened when the destructive step finishes or
fails, before the upgrade and seed connect. If the reset process is killed
inside the window, the database stays closed rather than unprotected. Once no
reset is running, the owner or a superuser reopens it while connected to another
database:

```sql
ALTER DATABASE payroll_v3 WITH ALLOW_CONNECTIONS true;
```

Inside the window the reset drops the Flussra application schemas and
`public.alembic_version` in one transaction. Before dropping anything it follows
PostgreSQL's dependency graph from those schemas.
If the drop would also remove any object outside them, such as a view, a column,
a domain, a constraint or an index elsewhere, it lists those objects and changes
nothing. Extensions and objects that do not depend on Flussra are kept. It then runs
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
