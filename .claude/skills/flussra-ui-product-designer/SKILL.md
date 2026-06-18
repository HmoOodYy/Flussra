# Skill: Flussra UI Product Designer

**Invoke this skill when:** planning, auditing, or designing any frontend/UI/UX work for Flussra — before writing any code.

---

## What Flussra Is

Flussra is **not** a generic dashboard, a CRUD app, or a consumer web app.

**Product direction:** Controlled, auditable driver payroll for transportation companies.

Operators (payroll admins, branch managers, company admins) use Flussra to:
- Record what work drivers did each day (Day Grid)
- Configure what drivers get paid (Pay Rates, Custom Daily Pay Items)
- Review and approve payroll before finalizing
- Produce an immutable auditable ledger after finalization

The main user is a **payroll operator or admin** — not a driver, not a casual consumer.

Drivers are **not necessarily login users**. They are subjects of payroll entries, managed by admin/manager functions.

---

## UI Tone and Direction

The UI must feel:

- **Calm** — no aggressive colors, no pulsing animations, no panic-inducing dashboards
- **Professional** — this is financial/payroll software; errors have real consequences
- **Modern SaaS** — clean, well-spaced, high-information-density where needed
- **Payroll-first** — every design decision should serve payroll workflow clarity
- **Operator-friendly** — reduce operator mistakes; surface the right action at the right time
- **Safe around irreversible actions** — finalization, deletion, and locking must be visually distinct and friction-gated

The UI must **not** feel:
- Flashy, decorative, or entertainment-product-like
- Crypto-style, cyberpunk, glassmorphism, or neon-accented
- Like a generic "AI-generated dashboard template"
- Overwhelming with raw backend fields surfaced directly
- Consumer-app friendly (dark mode toggle, emoji-heavy, casual language)

---

## Payroll Lifecycle — Must Be Reflected in UI

| State | What It Means | UI Requirement |
|---|---|---|
| **Draft / Open** | Period is editable work-in-progress | Normal edit affordances available |
| **InReview** | Period is locked for editing; awaiting review decision | Read-only banner; no edit affordances; clear locked state |
| **Approved** | Period approved, awaiting finalization | Finalize action available; no editing |
| **Locked / Finalized** | Immutable official result; ledger is authoritative | Read-only; ledger-style display; audit trail accessible |
| **Cancelled / Archived** | Historical; not actionable | Minimal display; no actions |

**Key rules:**
- Current Payroll = editable work-in-progress
- InReview = locked; must not be a moving target
- Ledger = official read-only output after finalization
- The backend enforces these states; the UI must surface them clearly

---

## Key Product Architecture Rules

### Status column
- There is **one Day Grid Status column** — the single place a payroll user selects a Status Key for a driver/day
- One selected Status Key → optional future allowance effect + optional future pay effect
- These are backend rule layers attached to the Status Key — not additional columns
- Do not propose adding a second Status column or a parallel status input field
- `StatusKeyPayRule` is a future reserved concept — not a current UI element

### SourceSnapshot / audit trail
- `SourceSnapshot` is recorded on every finalized line as a JSONB blob explaining how the amount was calculated
- The UI must present this as **human-readable audit details** — labelled fields, not raw JSON
- The ledger view is authoritative and immutable; design it accordingly

### Advanced rates

- Existing backend payroll trust flows support advanced rate evidence for supported system/default behaviors such as tiers, ranges, and blocks. When these appear in ledger/audit UI, they must be presented as human-readable calculation details — not raw backend codes or raw rate-type strings.
- For Custom Daily Pay Items (CDPI), the accepted current workflow is **PerUnit-only**. This is the only CDPI calculation method that is live and supported in the current frontend/backend workflow.
- Custom advanced methods such as slot-based CDPI, OrdinalTier, RangeBracket, RangeProgressive, and Block are future/planned architecture — they are not currently active product behavior.
- Do not design or implement UI that assumes custom advanced methods are live.
- If custom advanced methods are referenced at all (e.g. in a settings form), they must be clearly disabled or marked future-only unless a dedicated backend implementation phase has explicitly shipped them.
- Do not expose raw rate-type code strings (e.g. `CPMI`, `OrdinalTier`) to operators without labels and explanation.

### CDPI (Custom Daily Pay Items)
- Full workflow is implemented: request → review → approve → branch activation
- Do not re-architect CDPI without explicit approval
- Pay Items page has been restructured with internal tabs and integrated branch controls

### Driver Allowance Tracking (DAC)
- This is a **future-only architecture plan** (`docs/architecture/DRIVER_ALLOWANCE_TRACKING_FUTURE_PLAN.md`)
- Do not design any DAC UI, entitlement forms, allowance category UI, or usage ledger UI
- Do not mention DAC features as "coming soon" in any operator-facing copy unless explicitly instructed

---

## How to Respond When This Skill Is Active

Before writing any code or making any file changes, produce a **design brief** covering:

### 1. UX Intent
What is this feature/page trying to help the operator accomplish? What is the outcome they need?

### 2. Primary User and Task
Who is the user (payroll admin, branch manager, company admin)? What is their specific task? What do they already know walking in?

### 3. Information Hierarchy
What is the most important information on this page/view? What is secondary? What should be hidden until needed?

### 4. Payroll Lifecycle Considerations
Does this page interact with a payroll period? If so, what states are possible, and how does each affect what the user sees and can do?

### 5. Risk Notes
What could go wrong if the design is wrong? Are there irreversible actions? Can an operator accidentally modify a locked period? Are there permission-level implications?

### 6. Options (2–3 when useful)
Present 2–3 distinct design approaches at the pattern level (not pixel level). For each: layout concept, key interactions, tradeoffs.

### 7. Recommendation
Which option is recommended and why, given the Flussra payroll-first, operator-safe product direction.

### 8. Scorecard or Checklist
Rate the recommendation against:
- [ ] Calm / professional tone
- [ ] Payroll workflow clarity
- [ ] Locked/InReview state clearly communicated
- [ ] No raw backend fields surfaced
- [ ] Irreversible actions friction-gated
- [ ] Branch context visible
- [ ] Drivers treated as managed subjects, not login users

**Do not write implementation code unless explicitly asked after the design brief is approved.**

---

## Tooling Rules

- React + TypeScript + CSS Modules is the current stack — design within it
- Do not propose Tailwind, MUI, shadcn/ui, or a new component library without explicit approval
- Do not require Figma as a design gate
- v0 / Uizard may be used for inspiration mockup reference only — not production code
- Do not propose a full page redesign without selecting a specific page and getting approval first

---

## Canonical Reference Files

Always treat these as the source of truth:
- `AI_FRONTEND_HANDOFF_PAYROLL_APP.md` — current frontend handoff
- `docs/architecture/DRIVER_ALLOWANCE_TRACKING_FUTURE_PLAN.md` — future DAC plan (do not implement)
