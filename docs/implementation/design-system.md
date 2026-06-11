# Design System

Date: 2026-06-10
Status: Reviewed

Applies to the Fleet dashboard (Jinja + htmx). Pin these tokens before any screen work; reuse
this block verbatim in every screen spec and role prompt.

## References (taste anchors)
- Looks like: density and restraint of **Linear**, log readability of **Grafana Explore**.
- Aesthetic in one line: calm dark ops console — quiet surfaces, one signal color, monospaced
  truth.

## Typography
- Display/heading face: **Space Grotesk** — geometric, distinctive at 14–24px, not a slop
  default.
- Body face: **IBM Plex Sans** — engineered for UI density, pairs with Plex Mono.
- Mono face (logs, diffs, ids, costs): **IBM Plex Mono**.
- Scale (px): 12 / 14 / 16 / 20 / 24 / 32 (body = 14 in tables, 16 in prose; dashboards are
  table-heavy).
- Line-height: multiples of 4 (20 for 14px, 24 for 16px); max line length ~75ch in prose.

## Spacing (4/8pt grid)
- Allowed steps (px): 4 / 8 / 12 / 16 / 24 / 32 / 48.
- Rule: internal padding ≤ external margin; table row vertical padding 8, card padding 16.

## Color (tokens — explicit values, dark theme only for MVP)
- Background: `#0F1417` (near-black with green-grey cast — not pure black, not #fff).
- Surface raised: `#161D21`; surface overlay: `#1D262B`.
- Neutral text ramp: primary `#E6EAEC`, secondary `#9AA7AD`, muted `#5C6B72`.
- Border: `#26323A` (1px, no glow).
- Accent (one): `#33B6A8` (teal) — primary actions, focus rings, live indicators only.
- Status hues (badges/dots only, never large fills): running `#33B6A8`, idle `#9AA7AD`,
  waiting `#D9A036`, paused_budget `#D9A036`, failed `#D96459`, archived `#5C6B72`.
- Diff: additions `#3FA372` tint, deletions `#D96459` tint at 12% opacity backgrounds.
- No purple/indigo→blue gradients; no gradient text.

## Shape & Elevation
- Radius by role: controls 6px / cards 10px / pills full / status dots full.
- Elevation: e0 none (tables, timeline) / e1 card `0 1px 2px rgb(0 0 0 / 0.4)` /
  e2 popover `0 4px 16px rgb(0 0 0 / 0.5)`. Tinted black, never colored glows.

## Icons
- Set: **Lucide**, stroke 1.5px, size 16px in tables / 20px in headers. Never emoji.
- Event-type icons fixed: message=`message-square`, tool=`wrench`, git=`git-branch`,
  test=`flask-conical`, merge=`git-merge`, approval=`shield-check`, error=`octagon-alert`,
  budget=`coins`.

## Motion
- One reveal: timeline rows fade-slide in 120ms on SSE arrival. Status dot pulses only while
  `running`. Respect `prefers-reduced-motion` (disable both). Nothing else animates.

## States (required for every view)
- Default / Empty (explanatory line + single CTA) / Loading (skeleton rows, not spinners) /
  Error (plain-language sentence + retry button + technical detail behind expander) / Success.

## Accessibility
- WCAG AA on the palette above (verified: `#E6EAEC` on `#0F1417` = 13.9:1; `#9AA7AD` on
  `#0F1417` = 6.9:1; accent `#33B6A8` on bg = 6.2:1).
- Focus ring: 2px accent outline, 2px offset, on every interactive element.
- Full keyboard nav; approval approve/deny reachable and operable by keyboard.
- ARIA labels on all icon-only buttons; touch targets ≥ 44px on approval actions.
