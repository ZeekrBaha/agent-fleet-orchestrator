"""C4: README endpoint table drift guard.

Parses the '## REST API overview' table from README.md and asserts that
every row corresponds to a real route in the live Fleet app.

If a route is added or renamed without updating the README (or vice versa),
this test fails loudly instead of silently drifting.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_README = Path(__file__).parent.parent / "README.md"
_TABLE_HEADER_RE = re.compile(r"^\|\s*Method\s*\|")
_ROW_RE = re.compile(r"^\|\s*`(\w+)`\s*\|\s*`([^`]+)`\s*\|")


def _parse_readme_routes() -> list[tuple[str, str]]:
    """Extract (METHOD, path) pairs from the REST API table in README.md."""
    in_table = False
    routes: list[tuple[str, str]] = []
    for line in _README.read_text().splitlines():
        if _TABLE_HEADER_RE.match(line):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                in_table = False
                continue
            m = _ROW_RE.match(line)
            if m:
                routes.append((m.group(1).upper(), m.group(2)))
    return routes


def _live_routes() -> set[tuple[str, str]]:
    """Return (METHOD, path) for every route registered in the Fleet app."""
    import fleet.main as _main  # noqa: PLC0415
    result: set[tuple[str, str]] = set()
    for route in _main.app.routes:
        if hasattr(route, "methods") and route.methods:
            for method in route.methods:
                if method != "HEAD":
                    result.add((method, route.path))
    return result


def test_readme_routes_match_live_app() -> None:
    """Every row in the README endpoint table must exist in the live app.

    Catches: renamed paths, deleted endpoints, typos in method names.
    Does NOT require the README to list every route — only that listed
    routes are accurate.
    """
    readme_rows = _parse_readme_routes()
    assert readme_rows, "README table parse returned zero rows — check table format"

    live = _live_routes()
    drifted = [
        (method, path)
        for method, path in readme_rows
        if (method, path) not in live
    ]
    if drifted:
        live_sorted = sorted(f"  {m} {p}" for m, p in live if p.startswith("/api/"))
        drift_lines = "\n".join(f"  {m} {p}" for m, p in drifted)
        pytest.fail(
            f"README lists {len(drifted)} route(s) that don't exist in the app:\n"
            f"{drift_lines}\n\n"
            f"Live /api/ routes:\n" + "\n".join(live_sorted)
        )
