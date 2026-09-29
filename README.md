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

## Branching

- `main` — canonical, always-deployable branch
- Short-lived feature branches for implementation work, merged via pull request
- Tags mark historical/release milestones (e.g. `pre-refactor-baseline`)
