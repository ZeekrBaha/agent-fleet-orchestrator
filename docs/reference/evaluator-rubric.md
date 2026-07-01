> Source: harness-engineering (`templates/evaluator-rubric.md`), folded in 2026-07-01 as the source-of-truth for this repo's role-manifest conventions (Task T12, pipeline-consolidation).

# Evaluator Rubric

Use this rubric after implementation and before final acceptance. Score with the
`repo-engineering-review` evidence standard: every score needs a basis tagged
**confirmed** (ran it / saw it), **likely** (strong static signal), or
**needs-runtime-verification** (unproven). Unverified claims cap a category at 1.

| Category | Question | Score (0-2) | Evidence (confirmed / likely / needs-runtime-verification) |
| --- | --- | --- | --- |
| Correctness | Does the implemented behavior match the requested feature? |  |  |
| TDD discipline | Was each behavior driven test-first (failing test before code)? |  |  |
| Verification | Did the required checks run — suite + type/lint green, with counts? |  |  |
| Scope discipline | Did the session stay inside the chosen feature scope (no drive-by changes)? |  |  |
| Security | RLS on, server-side auth, no client/repo secrets, input validated? |  |  |
| Reliability | Does the result survive restart or rerun without repair? |  |  |
| Maintainability | Lean (< ~300 lines/module), clear names, conventions followed? |  |  |
| Handoff readiness | Can a fresh session continue from repo artifacts + `/context-restore` alone? |  |  |

## Verdict

- Accept  — all categories ≥ 1 and no failing validation gate
- Revise  — gaps are fixable in scope
- Block   — failing tests, missing evidence, or a security violation

## Required Follow-Up

- Missing evidence (what still needs runtime verification):
- Required fixes:
- Failing validation gates:
- Next review trigger:

<!-- Source: https://walkinglabs.github.io/learn-harness-engineering/en/resources/templates/ -->
