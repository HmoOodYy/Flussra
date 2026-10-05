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
assertion that Alembic has exactly one head at `0077`. It then runs every
frontend `tests/*.test.ts` file, ESLint, and the production build. A successful
exit means every required gate passed; the command stops and returns nonzero
when a gate fails or required tooling is missing. Install the worktree-local
backend environment with `.\scripts\setup_backend.ps1` and frontend
dependencies with `npm ci` from `frontend/` before validation.

During validation, the script verifies that `C:\Temp` is writable and sets
`TEMP` and `TMP` to that directory for its process and child tools only.

Future CI workflows should call this repository-owned entry point so local
and CI validation use the same contract.

## Branching

- `main` — canonical, always-deployable branch
- Short-lived feature branches for implementation work, merged via pull request
- Tags mark historical/release milestones (e.g. `pre-refactor-baseline`)
