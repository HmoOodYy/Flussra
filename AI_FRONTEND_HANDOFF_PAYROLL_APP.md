# AI Frontend Handoff — Payroll App v3 (Flussra)

**This is the current working context for AI assistants.**
**This file replaces all older handoff/context files, including the M18 `docs/handoff/` set.**

---

> **For AI assistants reading this file:**
> This is the canonical, up-to-date handoff. Do not treat older handoff files in `docs/handoff/` (if any remain) as current. If you find conflicting information between this file and older docs, trust this file.

---

## What This Product Is

This is **not** a generic dashboard or a CRUD app.

**Product direction:** Controlled, auditable driver payroll for transportation companies.

Operators run payroll for drivers who work routes. The system tracks:
- What work each driver did each day (Day Grid)
- What they should be paid (Pay Rates, Custom Daily Pay Items)
- That the result is correct and auditable (Finalization, Ledger, SourceSnapshot)
- That review periods have integrity (InReview lifecycle, change guards)

The product must feel **calm, professional, payroll-first, safe, and workflow-driven**. It is not a consumer app. Operators are payroll admins, branch managers, and company admins — not drivers.

---

## Current Focus

**Frontend and product workflow readiness — not more backend hardening.**

The backend payroll trust foundation is materially complete for beta-level trust:
- Finalization, ledger, and rate calculation are guarded and tested
- InReview state change guards are in place
- SourceSnapshot is recorded on all finalized lines for auditability
- CDPI (Custom Daily Pay Items) workflow is complete end-to-end

What still needs work is **making the backend trust understandable and usable for operators**. The UI must surface the right information at the right time, prevent operator mistakes, and make the audit trail accessible without requiring raw JSON inspection.

---

## AI Role Division

| Role | Responsibility |
|---|---|
| **ChatGPT** | Product/UX/prompt/planning role — designs phases, writes prompts, reviews product direction |
| **Claude** | Implementer — executes frontend phases based on approved prompts and plans |
| **Codex** | Strict reviewer — validates correctness, flags regressions, reviews architecture |

The user wants:
- Practical, scoped prompts
- Controlled phases with clear deliverables
- Honest reports (no inflated claims of completion)
- No surprise large rewrites

---

## Recommended Frontend Phase Order

| Phase | Name | Goal |
|---|---|---|
| **UI-0** | Design System Lite + UX Rules | Establish color tokens, spacing, typography, component conventions, and UX rules before touching any page. Prevents inconsistency in later phases. |
| **UI-1** | Driver Onboarding / Driver Management | Driver list, driver detail, onboarding flow. Drivers are the central subject of payroll. |
| **UI-2** | Review Lifecycle / InReview Read-Only | Make the InReview state visible and meaningful. Operators must understand what is locked vs. editable. InReview must not be a moving target. |
| **UI-3** | Ledger Audit Details | Surface SourceSnapshot and finalized line details in a human-readable audit UI. Not raw JSON. |
| **UI-4** | Pay Rates UX | Guided advanced rate configuration. Advanced rates are supported but must be guided — operators should not encounter raw rate-type codes without explanation. |
| **UI-5** | Permissions / Navigation Cleanup | Role-based UI visibility, navigation cleanup, branch/company scope clarity. |
| **UI-6** | Deployment Readiness Package | Final polish, error states, empty states, loading states, mobile responsiveness audit. |

**Do not jump into coding a large redesign before the user chooses the first target page and workflow for each phase.**

---

## Key Product Rules (Must Be Preserved in UI)

### Payroll lifecycle
- **Current Payroll** (draft/open period) = editable work-in-progress
- **InReview** = locked for editing; read-only; operators review before finalizing
- **Finalized / Ledger** = immutable official result after finalization; the ledger is the source of truth

### InReview integrity
- InReview must not allow writes from the normal edit path (write guards in place in backend)
- The UI must make it visually clear that an InReview period is not editable
- Review must not become a moving target — operators should not be able to accidentally modify a period under review

### Drivers
- Drivers are **not necessarily login users**
- Drivers appear in the Day Grid as subjects of payroll entries, not as authenticated app users
- Driver management is an admin/manager function

### Audit trail
- `SourceSnapshot` is recorded on every `PayrollFinalLine` as a JSONB snapshot of how the line was calculated
- This must be presented in **human-readable audit/details UI** — not raw JSON-first display
- The ledger is the official read-only result; it should be presented as authoritative and immutable

### Advanced rates
- Advanced rate types (CPMI, CPLD, slot-based CDPI, etc.) are supported in the backend
- The UI must **guide** operators through advanced rate configuration — do not expose raw rate-type codes without labels and explanations

### Custom Daily Pay Items (CDPI)
- Full CDPI workflow is implemented: request → review → approve → branch activation
- Branch-level display name override is supported
- The CDPI settings UI (Pay Items page) has been restructured: internal tabs, integrated branch controls
- Do not re-architect CDPI without explicit approval

---

## Design and Tooling Rules

- **Do not use Figma as a required workflow.** The team does not use Figma as a mandatory design gate.
- **v0 / Uizard** may be used for inspiration and mockup reference only — not as a source of production code.
- The UI framework is **React + TypeScript + CSS Modules** (existing stack). Do not introduce Tailwind or a new component library without explicit approval.
- **Do not jump into coding a large redesign** before the user selects the target page and approves the approach.

---

## What Is Already Built (Frontend)

The following frontend areas are implemented and should not be redesigned without explicit approval:

- Settings > Pay Items page (tabs, CDPI branch controls, item detail panel)
- CDPI request workflow (create, submit, decide, copy)
- Day Grid (payroll entry per driver per day)
- Pay Rates configuration
- Payroll period management (open, InReview, finalize)
- Branch and company selection context

---

## Future Architecture Plans

The **Driver Allowance Tracking (DAC)** system is accepted as a future backend architecture plan.

Canonical document: [`docs/architecture/DRIVER_ALLOWANCE_TRACKING_FUTURE_PLAN.md`](docs/architecture/DRIVER_ALLOWANCE_TRACKING_FUTURE_PLAN.md)

**DAC is not an active implementation task.** It must not be implemented until the core payroll app is stable and a dedicated DAC phase is explicitly approved.

---

## Active Constraints (All AI Assistants)

These constraints are in effect unless explicitly lifted by the user:

- Do not modify finalization logic, ledger writes, or rate calculation paths without explicit approval
- Do not change the CDPI API contract or CDPI backend behavior
- Do not rename CPI_ rate types or backfill CdpiDefinitions
- Do not delete legacy code or legacy data without explicit approval
- Do not implement DAC (Driver Allowance Tracking) — it is a future plan only
- Do not introduce new backend migrations without explicit approval
- AppShell and global navigation changes require explicit approval before implementation

---

*Last updated: 2026-06-18 · Replaces: `docs/handoff/` M18 set (PROJECT_HANDOFF, MILESTONES_STATUS, NEXT_STEPS, BACKEND_CURRENT_STATE, ARCHITECTURE_DECISIONS, FRONTEND_API_INVENTORY, FRONTEND_STARTUP_GUIDE, OPEN_ISSUES_AND_DEFERRED_DECISIONS)*
