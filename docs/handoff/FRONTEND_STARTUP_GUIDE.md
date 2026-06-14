# Frontend Startup Guide

Last verified: 2026-05-30 (M18 — Frontend Handoff / Backend Readiness Docs)

This guide is for the session that begins frontend development.
Read this alongside `FRONTEND_API_INVENTORY.md` before writing any frontend code.

---

## 1. Run the Backend Locally

From `C:\Projects\etbdnt\Payroll_App_v3\backend`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The server starts on: **http://127.0.0.1:8000**

- Swagger UI (interactive API docs): **http://127.0.0.1:8000/docs**
- OpenAPI JSON schema: **http://127.0.0.1:8000/openapi.json**
- The `--reload` flag auto-restarts on code changes.

---

## 2. Verify Alembic Head = 0013

From `C:\Projects\etbdnt\Payroll_App_v3`:

```powershell
python -m alembic current
```

Expected output includes: `0013 (head)`

If the local dev database is behind:

```powershell
python -m alembic upgrade head
```

If `alembic current` fails with a password error, fix the `payroll_user` credentials
in `backend\.env` first. The dev database has been manually upgraded to 0013 already.

---

## 3. Run the Full Backend Test Suite

From `C:\Projects\etbdnt\Payroll_App_v3\backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Expected: **734 passed, 1 skipped, 0 failed**

Run this before starting frontend work to confirm the backend is in the expected state.

---

## 4. Verify the Backend Is Responding

```powershell
curl http://127.0.0.1:8000/docs
```

Or open http://127.0.0.1:8000/docs in a browser. You should see the Swagger UI.

---

## 5. Auth Flow

### Step 1 — Login

```http
POST http://127.0.0.1:8000/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "YourPassword",
  "company_code": "DEMO"
}
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### Step 2 — Use the Token

Include the token in every subsequent request:

```http
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### Step 3 — Verify Token on App Load

```http
GET http://127.0.0.1:8000/auth/me
Authorization: Bearer <token>
```

Response includes:
```json
{
  "user_id": 1,
  "username": "admin",
  "display_name": "Admin User",
  "company_id": 1,
  "company_name": "Demo Logistics",
  "scope_type": "AllCompanyBranches",
  "branch_ids": []
}
```

Use `scope_type` and `branch_ids` to decide whether to show a branch picker:
- `AllCompanyBranches` → show branch picker (call GET /core/branches to populate it)
- `SpecificBranch` → branch is fixed; use `branch_ids[0]` directly

### Step 4 — Handle 401

On any 401 response: clear the stored token, redirect to the login screen.

---

## 6. Sending the Authorization Header

Every API call after login must include:

```
Authorization: Bearer <access_token>
```

Example with fetch:
```typescript
const response = await fetch('http://127.0.0.1:8000/payroll/periods', {
  headers: {
    'Authorization': `Bearer ${accessToken}`,
    'Content-Type': 'application/json',
  },
});
```

Example with axios:
```typescript
axios.defaults.headers.common['Authorization'] = `Bearer ${accessToken}`;
```

Or use an axios interceptor to inject the token automatically on every request.

---

## 7. Branch Scope — What the Frontend Must Know

Every data endpoint is scoped to the user's accessible branches.

### AllCompanyBranches users
- See all company data.
- Should show a branch filter/picker in the UI.
- Pass `?branch_id=N` as a query param where the API supports it.
- If no `branch_id` is passed, returns data for all branches.

### SpecificBranch users
- Only see data for their assigned branches.
- No branch picker needed — or show the branch name as a static label.
- The backend automatically filters to their branches.
- Payroll.entry permission is checked per-branch for dashboard access.

---

## 8. Key Backend Behaviors to Know Before Building

### Period lifecycle
Open → InReview is the key user-triggered submit action. It:
1. Requires at least one non-voided draft line.
2. Requires no `needs_manager_review=true` lines.
3. Atomically creates a PeriodApproval review item.
4. If a concurrent duplicate submit races in, returns 422 "A pending review already exists."

### Review drives period approval
InReview → Approved does NOT happen via PATCH /status.
It happens when a user with `review.decide` POSTs to `/review/items/{id}/decide`
with `{"decision": "Approved"}`. The backend wires the decision back to the period.

### Immutable final lines
Once a period is Locked, its lines cannot be changed.
Final lines are read-only via GET /payroll/periods/{id}/final-lines.
System lines (SYS_MIN_TOPUP, SYS_MAX_CAP) may appear if driver pay rules fired.

### needs_manager_review
A draft line gets `needs_manager_review=true` when no approved rate exists for the driver.
The UI should highlight these lines. The period cannot be submitted or finalized with
any such lines unless they are resolved (add an approved rate for the driver) or the
line has an explicit `rate_amount` set manually.

### Audit trail
All sensitive writes create audit rows. This is backend-handled — no frontend work needed.

---

## 9. Recommended First Frontend Prompt

When starting the first frontend session, use a prompt like this:

---

*"We are starting frontend development for a payroll management system. The backend is
complete and running at http://127.0.0.1:8000. Swagger docs are at /docs. All auth, payroll,
review, settings, admin, and dashboard endpoints are live.*

*Tech stack: React, TypeScript, Vite.*

*Start with:*
*1. Project scaffold (Vite + React + TypeScript)*
*2. Login screen — POST /auth/login, store JWT in memory/sessionStorage*
*3. Auth guard — redirect to login if no token*
*4. GET /auth/me on app load — verify token, get user profile*
*5. App shell — top nav with user name, logout button, sidebar placeholder*

*Read docs/handoff/FRONTEND_API_INVENTORY.md and docs/handoff/FRONTEND_STARTUP_GUIDE.md
before writing any code.*

*Do not connect to PostgreSQL directly. All data must come from the API.*"*

---

## 10. Environment Configuration for Frontend

The frontend will need the backend base URL. Recommended approach:

`.env.local` in the frontend project root:
```
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Reference in code:
```typescript
const API_BASE = import.meta.env.VITE_API_BASE_URL;
```

For production, override `VITE_API_BASE_URL` with the deployed backend URL.

---

## 11. CORS Note

If the React dev server and the backend are on different ports, the backend may need
CORS headers configured. Check `backend/app/main.py` for the CORSMiddleware setup.
If CORS is not yet configured, add it before the first frontend request:

```python
# In main.py
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite default
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

This is a backend task but may be needed on day one of frontend development.

---

## 12. Quick Reference

| What                     | Command / URL                                          |
|--------------------------|--------------------------------------------------------|
| Start backend            | `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload` (from `backend\`) |
| Run tests                | `.\.venv\Scripts\python.exe -m pytest` (from `backend\`) |
| Swagger UI               | http://127.0.0.1:8000/docs                             |
| OpenAPI JSON             | http://127.0.0.1:8000/openapi.json                     |
| Alembic current          | `python -m alembic current` (from project root)        |
| Alembic upgrade          | `python -m alembic upgrade head` (from project root)   |
| Login endpoint           | POST http://127.0.0.1:8000/auth/login                  |
| Auth header format       | `Authorization: Bearer <token>`                        |
| Expected test result     | 734 passed, 1 skipped, 0 failed                        |
| Alembic head             | 0013                                                   |
