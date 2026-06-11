"""Thin HTTP relay — calls the Fleet API on behalf of the MCP tool server.

The relay is the *only* network client in the tool server process.  It reads
FLEET_API_TOKEN from env at construction and attaches it as a Bearer header to
every request.  The Fleet API is authoritative for policy; the relay is untrusted.

Public API:
    ToolCallError               — raised on non-2xx responses
    FleetRelay(base_url, token) — HTTP relay client
    FleetRelay.call(tool_name, agent_id, scope, payload) -> dict
"""

from __future__ import annotations


class ToolCallError(Exception):
    """Raised when the Fleet API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Fleet API returned {status_code}: {detail}")


class FleetRelay:
    """Sends tool calls to the Fleet API over localhost HTTP.

    One instance is created per tool server process and shared across all
    tool calls.  Uses httpx.AsyncClient for async HTTP.
    """

    def __init__(self, base_url: str, token: str) -> None:
        import httpx

        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    async def call(
        self,
        tool_name: str,
        agent_id: str,
        scope: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """POST /api/tools/{tool_name} with validated payload.

        Args:
            tool_name: Name of the tool to invoke (matches API route).
            agent_id:  ID of the calling agent.
            scope:     Scope string for audit events.
            payload:   Validated input dict (already schema-checked by caller).

        Returns:
            Parsed JSON dict from the API response body.

        Raises:
            ToolCallError: If the Fleet API returns a non-2xx status.
        """
        url = f"{self._base_url}/api/tools/{tool_name}"
        full_payload = {"agent_id": agent_id, "scope": scope, **payload}

        resp = await self._client.post(
            url,
            json=full_payload,
            headers={"Authorization": f"Bearer {self._token}"},
        )

        if not (200 <= resp.status_code < 300):
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:  # noqa: BLE001 — best-effort detail extraction
                detail = resp.text
            raise ToolCallError(status_code=resp.status_code, detail=str(detail))

        result: dict[str, object] = resp.json()
        return result

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
