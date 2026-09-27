# AGENTS.md

Guidance for AI agents (and humans) working in this repository.
ArenaBench is a Python (uv) project — the benchmark arena core
(`arenabench/`), recorder, web app, and UI, plus infra and match
tooling. `make help` lists the entry points; `README.md` is the
authoritative source for the details.

## Agent-monitored pull requests

Mac set this on 2026-09-26 for every repository. The `agent-monitored-pr` label marks a PR that an agent watches until it merges or closes. A labelled PR comes before other work, and its fixes run in parallel wherever that is safe.

- **Label every PR an agent opens.** Pass `--label agent-monitored-pr` to `gh pr create`. If the repository has no such label, create it first: `gh label create agent-monitored-pr --color fd0880 --description "Agent polls every 60 seconds fixes CI, comments, conflicts."`
- **Poll the PR every 60 seconds.** Each poll reads the PR's state, its mergeability, the checks on the head commit, and every review thread with no inline reply after the reviewer's last comment. `gh pr view --json` does not return review threads, so read them with `gh api graphql` (`pullRequest.reviewThreads`).
- **Fix by review pass.** Pass N is the Nth review one reviewer submits on the PR. On pass 1, fix every P0, P1, and P2 finding. On pass 2, fix P0 and P1. From pass 3 on, fix P0 only. A P0 blocks the PR at every pass.
- **File one residue issue.** Carry every P1 and P2 finding left unfixed into a single issue for the PR. Its title ends with `(residue #<PR>)`, and its body links the PR. Reply inline on every thread you handle, with the commit that fixed it or a link to the residue issue.
- **Clear conflicts and CI failures as they appear.** When the PR conflicts, merge the base branch in, resolve it, and push. When a job fails, read its failing step with `gh run view --job <id> --log-failed`, fix it, and push without waiting for the rest of the run.
- **Dispatch subagents.** Give each independent fix its own subagent when no two fixes touch the same file. Stay active until the PR merges or closes.
- **Search for the label every 60 seconds.** A session that watches PRs runs `gh search prs --owner macanderson --label agent-monitored-pr --state open` every 60 seconds and takes each labelled PR that no live session owns. A PR has one writer. Two writers on one branch restart each other's CI and reject each other's pushes, so check `~/.claude/jobs/*/state.json` for an owner first, and message that session instead of pushing to its branch.

## Standing decisions — apply without asking

Each directive below is a Steering Context Record in [`docs/scr/`](docs/scr/);
the SCR is canonical — it carries the rationale, exceptions, and enforcement
status. This block is the compiled summary that every agent — Claude Code
(via CLAUDE.md's `@AGENTS.md` import) and Stella (which reads AGENTS.md
directly) — loads at session start. The corpus is identical across the
macanderson org repos.

- **[SCR-001](docs/scr/SCR-001-no-full-suite-builds.md) — Tests/builds
  (inner loop):** Never compile or run the full test suite while developing.
  Build and test only the crates/packages/modules touched by the change
  (plus direct dependents on interface changes). The full suite is CI's job.
  Here: `uv run pytest tests/test_x.py -q`, never bare `pytest` or `make test`.
- **[SCR-002](docs/scr/SCR-002-durability-first-architecture.md) —
  Architecture decisions:** Do not ask. Choose the most durable option — the
  one that can't be questioned in 10 years as the right move. Cheap-and-easy
  only wins when it is also the excellent durable choice. Record every such
  decision as an ADR in `docs/adr/`; the ADR replaces the question.
- **[SCR-003](docs/scr/SCR-003-dod-verified-close.md) — Definition of
  done:** An issue closes only when every DoD checklist item is satisfied
  and verified. Reference-grade includes tests, code comments, docs, and
  CI — not just the implementation. A PR that advances an issue without
  finishing it links it with `Refs #N` rather than `Closes #N`: `Refs`
  does not close, so the merge gate does not hold that PR against the
  issue's DoD. A PR may carry both, and is gated only on what it closes. A
  PR that closes nothing is waived by a label, and which one is a claim:
  `no-issue` for a trivial change, `closes-nothing` for a substantial one
  that closes no issue by design.
- **[SCR-004](docs/scr/SCR-004-residue-becomes-issues.md) — Fix over
  file:** Fix what you notice in the PR you are making; two unrelated fixes
  in one PR is fine. File an issue only when a fix cannot responsibly ride
  the PR (a maintainer decision, a rig or spend, or work larger than the
  session), and only when fixing it moves stability, reliability,
  maintainability, innovation, efficiency, or performance. Apply ONLY the
  `triage` label.
- **[SCR-005](docs/scr/SCR-005-triage-separation-of-duties.md) — Triage
  separation of duties:** Never apply priority (`P0`–`P3`) or size labels —
  a dedicated triage agent owns sizing and priority; a guard workflow
  strips creator-applied priorities.
- **[SCR-006](docs/scr/SCR-006-schema-changes-are-labelled.md) — Schema
  changes and migrations:** A pull request that changes a schema carries
  `migration-required`, and the migration reaches production before or with
  the deploy of that change, never after. Say in the PR which store changed
  and what must be applied. Where a repo has no automation to apply the label
  from the diff, apply it by hand. Do not add an automatic apply to a deploy
  pipeline under this record; that is a separate decision, made per repo.
