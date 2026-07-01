> Source: harness-engineering (`sops/layered-domain-architecture.md`), folded in 2026-07-01 as the source-of-truth for this repo's layering and role-manifest conventions (Task T12, pipeline-consolidation).

# Layered Domain Architecture

Use this SOP when the agent keeps crossing layer boundaries, duplicating logic
across layers, or producing code that reviews fine once but rots after a few
sessions. The fix is to make domain boundaries explicit enough that an agent can
move fast without quietly degrading structure.

## Goal

Define domain boundaries up front so any fresh agent knows which layer owns a
change before it writes a line of code.

## Target Model

Within a business domain, keep dependencies flowing in one direction:

```
Types -> Config -> Repo -> Service -> Runtime -> UI
```

- Each layer may depend only on layers to its left.
- Cross-cutting concerns (auth, telemetry, external APIs) enter through explicit
  providers or adapters, never ad hoc from inside a layer.
- Shared utils live outside the domain and stay generic. The moment a util grows
  domain logic, it belongs in the owning domain instead.
- **Sinks over pipes.** Within a layer, prefer components whose effects are
  contained and visible in the return value over components that trigger a cascade
  of downstream side effects. A fresh agent must be able to reason about a unit in
  isolation; an implicit trigger→effect chain forces it to hold the whole system.
  Where a pipe is genuinely needed, name the chain in `ARCHITECTURE.md` behind one
  declared boundary.

## Setup Checklist

- [ ] List the current domains in `ARCHITECTURE.md`.
- [ ] Write the allowed dependency directions in `ARCHITECTURE.md`.
- [ ] Record cross-cutting interfaces: auth, telemetry, external APIs.
- [ ] Add one short note on the hardest current boundary violation.
- [ ] Decide which rules to enforce mechanically (lint, tests, scripts).

## Execution SOP

1. Map the codebase into domains before debating implementation style.
2. For each domain, fix the allowed layer sequence.
3. Find every cross-cutting concern and route it through a provider or adapter.
4. Push ambiguous shared logic into either its owning domain or truly generic utils.
5. Document the rules in `ARCHITECTURE.md`.
6. Add one executable guardrail for the highest-cost violation.
7. Re-score quality after the change.

## Definition of Done

| Check | Pass condition |
| --- | --- |
| Ownership | A fresh agent can tell which layer owns a given change. |
| UI isolation | UI code no longer reaches into repos or external side effects directly. |
| Sinks not pipes | No module's behavior depends on side effects invisible in its interface; any real pipe is named behind one boundary. |
| Cross-cutting | Every cross-cutting concern has a named entry point. |
| Enforcement | At least one important boundary is enforced mechanically. |

## Artifacts to Update

- `ARCHITECTURE.md` — domains, layer order, dependency rules.
- `docs/QUALITY_SCORE.md` — re-scored after the change.
- `docs/design-docs/` — when the rationale shifted.
- `docs/PLANS.md` or the active execution plan.

## In my harness

- **Design the layers in the spec, not the code.** This SOP maps directly to the
  `architecture.md` phase of the `create-app-implementation-docs` skill. Define
  domains, the `Types -> Config -> Repo -> Service -> Runtime -> UI` flow, and
  cross-cutting adapters there before any implementation prompt runs. Boundaries
  decided in the spec-first docs are the ones agents actually respect.
- **Trace layers in existing code with serena MCP.** Use
  `get_symbols_overview` to map a domain, `find_symbol` to inspect a layer, and
  `find_referencing_symbols` to verify dependencies only point left (e.g. confirm
  no UI symbol references a repo symbol directly). This is the symbol-level audit
  of whether the target model holds.
- **Audit layering with `repo-engineering-review`.** Run it to check whether the
  declared boundaries are respected and whether lint/test guardrails exist for
  the highest-cost violation (step 6).
- **Mechanical guardrail.** Per global CLAUDE.md TDD, encode the most important
  boundary as a failing test first (e.g. assert no import path from `ui/` into
  `repo/`), then make it pass. That is the "enforced mechanically" Done check.
- **Plan the refactor** with `superpowers:writing-plans` and execute via
  subagent-driven-development so each layer move stays small and reviewable.

**Source:** https://raw.githubusercontent.com/walkinglabs/learn-harness-engineering/main/docs/en/resources/openai-advanced/sops/layered-domain-architecture.md
