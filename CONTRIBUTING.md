# Contributing

## Setup

```bash
# Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install dependencies
git clone https://github.com/ZeekrBaha/agent-fleet-orchestrator
cd agent-fleet-orchestrator
uv sync --all-extras
```

## Development workflow

All changes follow strict TDD:

1. **Write the test first** — one failing test describing the desired behavior
2. **Run it, watch it fail** — confirm it fails for the right reason
3. **Write minimal code to pass** — simplest thing that works
4. **Run again, watch it pass** — and verify existing tests stay green
5. **Refactor if needed**, keeping tests green

Never write production code without a failing test first.

## Quality gates (must all pass before pushing)

```bash
uv run ruff check fleet/ tests/   # lint
uv run mypy fleet/                # type-check
uv run pytest -q                  # full test suite
```

CI enforces the same three checks on every push and PR.

## Commit style

Follow the existing convention:

```
fix(scope): short description
feat(scope): short description
chore(scope): short description
```

## Branch strategy

- `main` — stable, CI-green at all times
- Feature/fix branches → PR → review → merge

Do not merge a PR until CI is green and at least one review has been completed.
