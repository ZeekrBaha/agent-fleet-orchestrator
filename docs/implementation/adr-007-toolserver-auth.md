# ADR-007 — Tool Server Authentication

**Status:** Accepted  
**Date:** 2026-06-10  
**Deciders:** Fleet Platform Team

---

## Context

The Fleet tool server (`fleet/toolserver/main.py`) is an MCP stdio process that runs
alongside the Fleet API server.  Agents connect to it via the MCP stdio protocol;
it in turn relays tool calls to the Fleet API over localhost HTTP.

The tool server is a **separate, untrusted process**.  It has no direct database
access and no ability to enforce fleet-wide policy.  All policy checks happen
server-side in the Fleet API.

Two authentication concerns arise:

1. **Tool server → Fleet API**: The relay process must prove its identity to the
   Fleet API so that unauthenticated callers cannot invoke tool endpoints directly.
2. **Agent → Tool server**: The MCP stdio channel is process-local; no
   over-the-wire credential is needed for this leg in the MVP.

---

## Decision

### Shared `FLEET_API_TOKEN` for relay→API auth

- A single `FLEET_API_TOKEN` environment variable is set on the tool server
  process at launch (via the same mechanism as the API server — e.g., systemd
  environment file, `.env`, or container secret mount).
- `FleetRelay` reads this token at construction time and attaches it as an
  `Authorization: Bearer <token>` header on every outbound HTTP request.
- The Fleet API validates the token using `hmac.compare_digest` (constant-time
  comparison) against its own `FLEET_API_TOKEN` setting — the same token both
  processes share.
- The token is **never logged, stored in the event database, or echoed in API
  responses**.  The `_scrub_payload` function in `fleet/api/tools.py` strips any
  key matching `(token|secret|password|api_key|auth)` before persisting audit
  events.

### Policy is API-side, not relay-side

- The relay is treated as untrusted infrastructure; it does nothing beyond
  forwarding validated payloads and the caller's `agent_id` / `scope`.
- Rate limiting, per-agent budget enforcement, scope isolation, and any
  future RBAC rules are implemented in the Fleet API layer, not in the relay.

### One process, all agents

- A single tool server process serves all agents.  Each tool call includes
  `agent_id` and `scope` fields in the request body; the API uses these for
  per-agent audit events and policy checks.

---

## Consequences

### Positive

- **Simple**: one shared secret, no per-agent credentials, no PKI.
- **Auditable**: every tool call and its result are recorded as `tool_call` /
  `tool_result` events in the Fleet event log with the calling `agent_id`.
- **Tamper-resistant**: constant-time token comparison prevents timing attacks.
- **Secret-safe**: token is never persisted; `_scrub_payload` guards audit events.

### Negative / Trade-offs

- **Token rotation requires process restart**: changing `FLEET_API_TOKEN`
  requires restarting both the API server and the tool server.  Acceptable
  for MVP where deployments are infrequent.
- **Single shared secret is coarse-grained**: all relay traffic uses the same
  credential.  A compromised tool server process can call any tool on any agent.
  Mitigation: the tool server runs in a restricted process environment; future
  work (ADR-008) will introduce per-scope tokens if needed.
- **No agent→toolserver auth in MVP**: the MCP stdio channel is process-local,
  so no credential is needed.  If the tool server is ever exposed over the
  network this decision must be revisited.

---

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| Per-agent JWT tokens | Adds key-management complexity with no benefit for a single-process relay |
| mTLS between relay and API | Too heavy for an MVP local-loopback connection |
| No auth (open API) | Violates least-privilege; API must reject unauthenticated callers |
| API key per tool | Same management burden as per-agent tokens with little isolation gain |

---

## Implementation notes

- `fleet/toolserver/relay.py` — `FleetRelay` reads token from env; attaches header.
- `fleet/api/auth.py` — `require_token` dependency validates with `hmac.compare_digest`.
- `fleet/api/tools.py` — `_scrub_payload` removes secret values before event storage.
- `fleet/config.py` — `Settings.secret_patterns` lists key patterns to scrub.
