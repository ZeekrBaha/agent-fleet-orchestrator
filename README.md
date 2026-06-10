# Fleet — Persistent AI Agent Orchestration Platform

Manage long-lived AI coding agents as a coordinated team: orchestrators plan and delegate,
workers execute in isolated git worktrees, every action lands in an append-only event log,
and merges are gated by validation evidence — not vibes.

This repository currently contains the **specification package** (no code yet).
Implementation follows the phased plan and is gated by the approval checklist in
`docs/implementation/implementation-plan.md`.

## Document map

| Doc | Purpose |
|---|---|
| `docs/implementation/research.md` | Goal, users, evidence, constraints, risks, options |
| `docs/implementation/requirements.md` | Numbered functional/non-functional requirements + acceptance criteria |
| `docs/implementation/design.md` | Flows, views, data model, failure modes, security |
| `docs/implementation/design-system.md` | Dashboard design tokens (pinned before any UI work) |
| `docs/implementation/architecture.md` | Module boundaries, ADRs, constraints |
| `docs/implementation/implementation-plan.md` | Phased task checklist with tests + validation commands |
| `docs/implementation/agent-assignments.md` | Which role prompt owns which tasks |
| `docs/implementation/validation-plan.md` | Gates and commands that define "done" |
| `docs/implementation/validation-report.md` | Filled after implementation (stub now) |
| `docs/prompts/*.md` | Role prompts for team-lead / developer / junior / tester / reviewer agents |

## Quick orientation

- Stack: Python 3.12+, FastAPI, SQLite (WAL), Pydantic v2, uv, MCP stdio tools, git CLI, SSE.
- Iron rules: TDD (failing test first), event for every action, fail-closed tool policy,
  no destructive git action without an explicit policy path and test coverage.
- Start building at Phase 1 only; later phases are blocked behind acceptance criteria.
