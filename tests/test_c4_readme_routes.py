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
_DASH_TABLE_HEADER_RE = re.compile(r"^\|\s*View\s*\|")
_DASH_ROW_RE = re.compile(r"^\|[^|]+\|\s*`([^`]+)`\s*\|")


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


def _parse_readme_dashboard_urls() -> list[str]:
    """Extract URL paths from the '### Web dashboard' view table in README.md."""
    in_table = False
    urls: list[str] = []
    for line in _README.read_text().splitlines():
        if _DASH_TABLE_HEADER_RE.match(line):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                in_table = False
                continue
            m = _DASH_ROW_RE.match(line)
            if m:
                urls.append(m.group(1))
    return urls


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


def test_readme_dashboard_urls_match_live_app() -> None:
    """Every URL in the README §5 dashboard view table must be a live GET route.

    Catches: renamed dashboard paths and wrong param names in the table.
    """
    urls = _parse_readme_dashboard_urls()
    assert urls, "README dashboard table parse returned zero rows — check table format"

    live_get_paths = {path for method, path in _live_routes() if method == "GET"}
    # Router paths have no trailing slash except the root mount.
    drifted = [
        url for url in urls
        if url not in live_get_paths and url.rstrip("/") not in live_get_paths
    ]
    if drifted:
        dash_sorted = sorted(p for p in live_get_paths if p.startswith("/dashboard"))
        pytest.fail(
            f"README dashboard table lists {len(drifted)} URL(s) that don't exist:\n"
            + "\n".join(f"  {u}" for u in drifted)
            + "\n\nLive /dashboard GET routes:\n"
            + "\n".join(f"  {p}" for p in dash_sorted)
        )


def test_all_api_routes_documented_in_readme() -> None:
    """Every live GET/POST route under /api/ must appear in the README table.

    Catches: a whole new router shipping with zero documentation (the
    accuracy-only check above cannot see routes the README never mentions).
    """
    documented = {path for _method, path in _parse_readme_routes()}
    undocumented = sorted(
        f"  {method} {path}"
        for method, path in _live_routes()
        if path.startswith("/api/")
        and method in ("GET", "POST")
        and path not in documented
    )
    if undocumented:
        pytest.fail(
            f"{len(undocumented)} live /api/ route(s) missing from the README table:\n"
            + "\n".join(undocumented)
        )
