# Skill: Flussra UI Implementation Guard

**Invoke this skill before any frontend implementation for Flussra.**

This skill enforces safe, scoped implementation behavior. It prevents accidental breakage of payroll behavior, permissions, branch scoping, or route protection when making frontend changes.

---

## Hard Constraints — Always Active

These constraints are in effect for all frontend work unless **explicitly lifted by the user in the current session**:

### Payroll and calculation behavior
- Do not change payroll calculation behavior
- Do not modify finalization logic, ledger write paths, or rate calculation
- Do not change day-grid save behavior
- Do not alter `FinalizationPreviewDialog` or `FinalSummaryDialog` logic without explicit approval

### Backend
- Do not modify any backend Python files
- Do not modify any Alembic migration files
- Do not create new API endpoints
- Do not change existing API client files (`src/lib/`) unless the task explicitly requires it
- Do not change response type definitions in a way that breaks existing API consumption

### Permissions and security
- Do not weaken permission checks
- Do not make hidden UI elements visible to users who should not see them
- Do not make disabled actions clickable for users who do not have permission
- Do not remove `PermissionGate` wrappers from routes
- Do not remove or bypass `ProtectedRoute`
- Do not change `authStore` or `PermissionGate` behavior

### Branch scoping
- Do not remove branch context from pages that currently display it
- Do not change "All Branches vs. single branch" logic in any page
- Do not change how branch selectors filter data

### Route protection
- Do not change route structure in `App.tsx` without explicit approval
- Do not remove permission gates from any route in `App.tsx`
- Do not change `SettingsDefaultRedirect` logic

### Form behavior
- Do not change form submission logic unless the task explicitly modifies it
- Do not change field validation rules unless the task explicitly modifies them
- Do not change required/optional field behavior
- Do not change what gets sent in API POST/PATCH payloads

### Driver Allowance Tracking (DAC)
- Do not implement any DAC feature
- DAC is a future-only architecture plan (`docs/architecture/DRIVER_ALLOWANCE_TRACKING_FUTURE_PLAN.md`)
- Do not create AllowanceCategories UI, entitlement forms, DAC ledger views, or any DAC-related component

### Status column architecture
- Do not alter the Day Grid Status column structure
- There is one Status column; do not add a second or parallel status input
- `StatusKeyPayRule` is a future reserved concept — do not implement it
- Do not change `PayrollStatusKeys` configuration UI behavior

### Scope discipline
- Implement only the approved page or phase
- Do not redesign every page at once
- Do not refactor unrelated components while implementing a targeted change
- Do not introduce new dependencies (npm packages) without explicit approval
- Do not introduce Tailwind, MUI, shadcn/ui, or any new component library

### Pre-existing modified files
- **Do not touch `frontend/src/pages/settings/pay-items/PayItemsPage.tsx`** unless the current task explicitly names it
- **Do not touch `frontend/src/pages/settings/pay-items/PayItemsPage.module.css`** unless the current task explicitly names it
- These files currently have pre-existing uncommitted modifications; accidental changes would corrupt the work-in-progress

### AppShell
- Do not modify `AppShell.tsx` or `AppShell.module.css` without explicit approval
- Navigation structure changes require explicit approval

---

## Required Pre-Implementation Checklist

Before writing the first line of code for any frontend task, confirm:

- [ ] The task is scoped to a specific page or component — not "all pages"
- [ ] No payroll calculation paths are in scope
- [ ] No permission logic is being changed
- [ ] No backend files are in scope
- [ ] No migration files are in scope
- [ ] `PayItemsPage.tsx` and `PayItemsPage.module.css` are out of scope
- [ ] The change does not alter form submission or API payload behavior (unless task explicitly requires it)
- [ ] DAC is not being implemented

If any item cannot be confirmed, stop and ask the user to clarify scope before proceeding.

---

## Required Post-Implementation Report

After every frontend implementation, return a structured report:

### Files changed
List every file that was created or modified. For each file:
- What was the change?
- Why was this file changed (what did the task require that touched this file)?

### Files explicitly not changed
Confirm that the following were not touched (or list any that were touched and why the task required it):
- `frontend/src/pages/settings/pay-items/PayItemsPage.tsx`
- `frontend/src/pages/settings/pay-items/PayItemsPage.module.css`
- `frontend/src/components/AppShell.tsx`
- `frontend/src/components/AppShell.module.css`
- Any file in `backend/`
- Any file in `backend/alembic/`
- Any file in `frontend/src/lib/` (unless explicitly required)
- `frontend/src/App.tsx` (unless explicitly required)

### Behavior preserved
State explicitly which existing behaviors were verified to be unchanged:
- Payroll calculation paths: unaffected / not in scope
- Permission gates: unaffected / not in scope
- Form submission and validation: unaffected / not in scope
- Branch scoping: unaffected / not in scope
- Route protection: unaffected / not in scope

### Validation run
Report the result of:
```bash
cd frontend && npm run build
```
Or if build was not run, state why and what was run instead. TypeScript errors must be zero before reporting complete.

### Git status
Paste `git status --short` output.

### Risk notes and follow-up
Call out any of the following if present:
- Any CSS change that could affect layout beyond the targeted component
- Any TypeScript type change that affects more than the targeted component
- Any z-index or overlay change that could affect modal stacking
- Any new file that introduces a new pattern that will need to be applied elsewhere
- Any pre-existing technical debt noticed (do not fix unless asked — just note it)

---

## Scope-Creep Signals — Stop If You See These

If you notice yourself doing any of the following, stop and ask the user:

- Editing more than 3 files that weren't mentioned in the task
- Refactoring a component "while I'm here" that wasn't in scope
- Adding error handling for scenarios not in the current task
- Changing a shared component that affects more than the target page
- Updating TypeScript types in `src/types/` for reasons beyond the task
- Touching `App.tsx` for reasons beyond the task
- Adding a new npm package

These are not always wrong, but they require explicit user confirmation before proceeding.

---

## Canonical Reference Files

Always treat these as the source of truth:
- `AI_FRONTEND_HANDOFF_PAYROLL_APP.md` — current frontend handoff and active constraints
- `docs/architecture/DRIVER_ALLOWANCE_TRACKING_FUTURE_PLAN.md` — future DAC plan (do not implement)
