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

### Backend test commands

`test_backend.ps1` defaults to the quick backend selection with two parallel
workers. It sets process-local `TEMP`/`TMP` through the same temporary-directory
authority as canonical validation and restores them when it exits.

```powershell
# One area, with its real fixtures and assertions
.\scripts\test_backend.ps1 --suite focused -- tests/test_period_entry_overview.py -x
# Everyday backend checks
.\scripts\test_backend.ps1 --suite quick
# Every retained backend test, with whole files kept on the same worker
.\scripts\test_backend.ps1 --suite full
# One of four duration-balanced CI partitions (indexes 0 through 3)
.\scripts\test_backend.ps1 --suite full --workers 1 --shard-index 0 --shard-count 4
```

Use `--workers 1` to debug serially. On non-Windows hosts, run the worktree's
backend Python with `scripts/run_backend_tests.py` and the same arguments.
`--plan <path>` records selected node IDs; `--junitxml <path>` records results
and timings. `--collect-only` verifies selection without running tests.
File estimates in `backend/tests/runtime_weights.json` balance partitions; new
files are included automatically with a default estimate. Each retained file
belongs to exactly one partition, and all tests in that file stay together.

The quick selection runs database-free cases plus representative auth, scope,
entry, Status, submission, finalization and whole-period-read database suites.
The full selection and schema-authority comparison remain required in CI.
Pure calculations and mapping rules need no database; PostgreSQL constraints,
real transactions, authorization and concurrency keep database-backed coverage.
Pristine schema templates are prepared once per worker and cloned for isolated
scenarios. Authentication/provisioning tests retain real login paths; business
tests may seed actors and use signed tokens while checking real authorization.

## Development database reset and canonical demo

Flussra's pre-production database support policy is reset/reseed. Populated
historical database upgrades, legacy backfills and historical downgrades are
not supported compatibility contracts. Fresh installation still executes the
entire migration chain, and tests retain current schema integrity, canonical
seed/reset, and equivalence of the SQL and Alembic schema authorities.

The development database is disposable infrastructure. A reset replaces the
whole database: nothing created inside it survives, so keep anything you need
elsewhere. The repository's migrations and the canonical demo seed are the only
sources it is rebuilt from:

1. `DROP DATABASE`, then `CREATE DATABASE ... TEMPLATE template0 ENCODING 'UTF8'`;
2. grant the application role its privileges;
3. `alembic upgrade head`, and assert the single Alembic head;
4. run the startup schema guard;
5. write the canonical demo (`scripts/canonical_demo.py`) and verify it read-only.

Run it from the `backend` directory with a separate lifecycle connection. That
connection goes to a control database on the same server (for example
`postgres`) as a role that can drop and create the development database: a
superuser, or a role with `CREATEDB` that owns the database. Supply the password
through `PGPASSWORD` or a pgpass file, never in repository configuration:

```powershell
.\.venv\Scripts\python.exe scripts\reset_dev_database.py --admin-url postgresql://postgres@localhost:5432/postgres --confirm payroll_v3
```

Roles and privileges:

| Role | Does | Needs |
|---|---|---|
| Lifecycle role (`--admin-url`) | Drops and creates the development database, which it then owns | `SUPERUSER`, or `CREATEDB` plus ownership of the existing database |
| Application role (`DATABASE_URL`) | Runs the migrations, the seed and the FastAPI backend, and owns every Flussra schema and object | `CONNECT`, `TEMPORARY` and `CREATE` on the database; `USAGE` and `CREATE` on schema `public` (granted by the reset); no `CREATEDB` |

The lifecycle credential is never part of the application configuration, and the
application credential cannot drop or create databases.

Before anything is dropped, all of these must hold:

- `ENVIRONMENT=development` is set explicitly in the environment or `.env` (the
  application default does not count);
- `--admin-url` is supplied (the reset never falls back to `DATABASE_URL`), it
  connects to a database other than the target, and its role can create
  databases;
- the target is the database the application actually connects to, and
  `--confirm` repeats that name. The reset resolves the target with SQLAlchemy's
  asyncpg dialect, as the backend, Alembic and the seed do, so query arguments
  such as `?database=`, `?user=` or `?host=` count. The application connection
  must then report that database and role;
- the lifecycle connection and the application credential reach the same
  PostgreSQL cluster. Both report `pg_catalog.pg_control_system().system_identifier`,
  and the two values must match; host names play no part. Network location is not
  a trust boundary: local, Docker, WSL, remote, CI and cloud development databases
  all work. When the target does not exist, the application side connects to the
  control database instead, so the application role needs `CONNECT` on it (the
  PostgreSQL default for `postgres`);
- an existing target is owned by the lifecycle role or the application role;
- no other session is connected to the target. Stop the backend and any other
  client first: an open session refuses the reset, which never terminates
  sessions and never uses `DROP DATABASE ... FORCE`.

Every connection the reset opens starts with the search path
`pg_catalog, pg_temp`, overriding database and role settings. Every system
catalog and function it uses is named with `pg_catalog`. So objects that the
application role, or anyone else, creates in other schemas cannot change these
checks.

`DROP DATABASE` and `CREATE DATABASE` are not one transaction. If creating or
rebuilding fails after the drop, the reset says so, and the database is absent
or incomplete. Fix the cause and run the reset again; it also creates the
database when it does not exist. `--verify-only` changes nothing and checks that
the database configured in `DATABASE_URL` is still the canonical demo.

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

The command runs the full backend pytest suite with two workers, Ruff, Python compileall, and an
assertion that Alembic has exactly one head at `0094`. It then runs every
frontend `tests/*.test.ts` file, ESLint, and the production build. A successful
exit means every required gate passed; the command stops and returns nonzero
when a gate fails or required tooling is missing. Install the worktree-local
backend environment with `.\scripts\setup_backend.ps1` and frontend
dependencies with `npm ci` from `frontend/` before validation.

During validation, the script verifies that `C:\Temp` is writable and sets
`TEMP` and `TMP` to that directory for its process and child tools only. It
restores the caller's original values and removes its invocation-owned
compileall cache directory when validation ends.

GitHub Actions runs four duration-balanced backend partitions on pull requests
to `main` and on manual dispatch. Each job owns its PostgreSQL cluster and its
test files. Static/frontend validation uses `validate.ps1 -SkipBackendTests`,
and schema authority equivalence runs independently. The existing **Canonical
Validation** check requires all partitions, static/frontend validation and
schema equivalence to succeed; it fails if any required job fails or is skipped.
Local canonical validation continues to run every gate in one command.

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
