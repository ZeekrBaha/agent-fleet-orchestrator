> Source: harness-engineering (`templates/quality-document.md`), folded in 2026-07-01 as the source-of-truth for this repo's role-manifest conventions (Task T12, pipeline-consolidation).

# Quality Document

A quality snapshot for each product domain and architectural layer. Both agents
and humans use this to see where the codebase is strong and where it needs work.
Grades carry the `repo-engineering-review` evidence standard — back each grade
with a basis tagged **confirmed / likely / needs-runtime-verification**.

**Update cadence:** After each significant session, or before starting a new
phase of work. Run `/repo-engineering-review` to refresh.

**Grading scale:**

- **A**: All verification passing, clean lean architecture, agent-legible, stable tests, security gates met (RLS, server-side auth, no client secrets)
- **B**: Verification passing, mostly clean, minor gaps in legibility or test coverage
- **C**: Partially working, known gaps, some areas hard for agents to understand
- **D**: Not working, or major structural / security issues

---

## Product Domains

> Replace these example domains with yours.

| Domain | Grade | Verification | Agent Legibility | Test Stability | Security (RLS/Auth) | Key Gaps | Last Updated |
|--------|-------|-------------|-----------------|---------------|--------------------|----------|-------------|
| Auth & Sessions | - | - | - | - | - | - | - |
| `Core Feature A` | - | - | - | - | - | - | - |
| `Core Feature B` | - | - | - | - | - | - | - |
| Data / Persistence | - | - | - | - | - | - | - |
| Billing / Payments | - | - | - | - | - | - | - |

## Architectural Layers

> Default layers for Next.js App Router + Supabase. Adjust to your stack.

| Layer | Grade | Boundary Enforcement | Agent Legibility | Key Gaps | Last Updated |
|-------|-------|---------------------|-----------------|----------|-------------|
| Server Components / Routes | - | - | - | - | - |
| Server Actions / API | - | - | - | - | - |
| Client Components | - | - | - | - | - |
| Data Access (Supabase + RLS) | - | - | - | - | - |
| Shared lib / utils | - | - | - | - | - |

## Change History

### YYYY-MM-DD

- Changes:
- Domains promoted:
- Domains demoted:
- New gaps identified:
- Gaps closed:
- Security findings:

<!-- Source: https://walkinglabs.github.io/learn-harness-engineering/en/resources/templates/ -->
