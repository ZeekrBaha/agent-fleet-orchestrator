> Source: harness-engineering (`templates/clean-state-checklist.md`), folded in 2026-07-01 as the source-of-truth for this repo's role-manifest conventions (Task T12, pipeline-consolidation).

# Clean State Checklist

> Run before ending a session or merging. Aligns with the `bash-guard` hook,
> the `finishing-a-development-branch` flow, and basic git hygiene. Every box
> must be checked (or explicitly waived with a reason) before the repo is "clean".

## Verification & State

- [ ] The standard startup path (`./init.sh`) still works.
- [ ] The standard verification path still runs.
- [ ] Full test suite is green; type check and lint are green (counts recorded).
- [ ] Every new behavior was driven test-first (RED→GREEN→REFACTOR).
- [ ] Current progress is recorded in `claude-progress.md` and `/context-save`.
- [ ] Feature state in `feature_list.json` reflects what is actually passing
      versus unverified.
- [ ] No half-finished step is left undocumented.
- [ ] The next session can continue without manual repair.

## Security

- [ ] No secrets in the diff, repo, or client bundle (env vars only).
- [ ] Supabase RLS is on for new/changed tables; auth stays server-side.

## Git Hygiene

- [ ] On a feature branch, not the default branch.
- [ ] No stray debug code, commented-out blocks, or `console.log` left behind.
- [ ] `git status` is clean (no unintended untracked or modified files).
- [ ] Commit message is descriptive; destructive ops were `bash-guard`-justified.
- [ ] Branch is ready for `finishing-a-development-branch` (merge / PR / cleanup).

<!-- Source: https://walkinglabs.github.io/learn-harness-engineering/en/resources/templates/ -->
