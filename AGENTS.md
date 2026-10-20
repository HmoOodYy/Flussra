# Core Executor Contract

- Before doing work, read the repository's authoritative agent instructions and the authoritative files/plans identified in the task prompt. Only inspect additional authoritative documentation
  when necessary to complete the task correctly; do not read the project's entire documentation set by default. Do not assume that a document is current or authoritative merely because it exists, where it is located, or how it is named; some documentation may be historical or superseded. If there is reasonable doubt about a document's current validity or authority, inspect its Git commit/history context and relevant newer changes to determine whether it is current, historical, or superseded before relying on it. If documentation conflicts with the current implementation, tests, other authoritative documents, or the task's explicitly agreed decisions, do not silently choose one. Use Git history and the surrounding evidence to determine whether the difference is intentional, stale, or unresolved; ask only if the authoritative state still cannot be established.

- Never invent missing requirements or make material assumptions silently. First use the task's authoritative sources, current implementation, tests, and relevant Git history
  to resolve factual uncertainty. If a material ambiguity still remains, or resolving it requires a product, domain, architecture, or scope decision rather than repository evidence, stop and ask.

- Stay within the requested scope. Do not perform unrelated cleanup, redesign, refactoring, or documentation work unless it is required to complete the task correctly.

- For a new substantive implementation task, use a dedicated task branch and isolated worktree. If the execution environment has already provided a suitable isolated branch/worktree for this task, use it rather than creating another one. Reuse the same branch/worktree when continuing the same task or fixing findings from its review. Read-only investigation or review does not require a new worktree.

- Inspect Git state before changing anything. Never overwrite, discard, or interfere with unrelated existing work.

- The executor owns local commits. Commit at meaningful, coherent checkpoints when the repository is internally consistent. Do not commit every trivial edit and do not force unrelated work into one commit.

- Do not push, create a PR, or merge unless explicitly instructed. Those steps are handled outside the normal execution session.

- Use focused tests and checks relevant to the changed area by default. Do not run a full suite routinely.

- If the task prompt explicitly says a full suite is critically required during active implementation, full-suite execution is a special exception to all normal subagent restrictions or limits. Use a separate, minimal, low-cost runner dedicated only to launching and monitoring the full-suite command. This runner does not count against any normal subagent allowance and may be created even when the task otherwise permits no subagents.

- Give that runner only the minimum context needed to run the command correctly. It must not perform implementation, investigation, review, architectural reasoning, or unrelated work. Its only responsibility is to run the suite, monitor it, and report the final result plus any meaningful failures or warnings back to the main responsible agent.

- If the full suite is only the final confidence check after focused tests have passed and implementation work is otherwise complete, do not run it through an agent or runner; leave it for the user to run manually.

- Never claim work is complete or correct without fresh verification evidence appropriate to the change.

- Except for dedicated agent-instruction files such as AGENTS.md, durable repository and Git artifacts must describe product and engineering reality, not the AI workflow. This includes production code, tests, documentation, branch names, commit messages, and PR metadata. Do not introduce model/tool names, agent roles, or temporary execution-plan/stage labels into those artifacts. Tests should be named after the behavior they verify. Preserve legitimate business, architecture, migration, roadmap, or historical terminology when it is genuinely part of the project.

- Update durable documentation only when the source of truth genuinely changed. Do not create redundant progress reports, AI notes, or unnecessary planning files.

- If the task is a review, treat it as read-only unless explicitly told to fix findings.

- At the end, report concisely:
  - what changed
  - important decisions or deviations
  - verification performed and results
  - commits created
  - unresolved risks or questions