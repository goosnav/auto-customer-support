"""Read-only order lookup boundary.

A conversation ID, email typed into chat, or caller ID is not verified identity.
Only a trusted server-side integration may create a VerifiedIdentity after its
own authentication or one-time-code flow. The public widget never does this.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

import httpx


@dataclass(frozen=True)
class VerifiedIdentity:
    customer_id: str
    email: str

    def __post_init__(self):
        if not self.customer_id.strip() or "@" not in self.email or "\n" in self.email or "\r" in self.email:
            raise ValueError("A verified customer ID and email are required")


class OrderConnector:
    """Read-only contract, suitable for a future store/MCP gateway adapter."""

    async def lookup(self, order_id: str, identity: VerifiedIdentity | None = None) -> dict[str, Any]:
        if identity is None:
            return {"status": "verification_required"}
        return {"status": "not_connected"}


class JsonOrderConnector(OrderConnector):
    """Local, optional order export. Never enabled merely by supplying an order ID."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    async def lookup(self, order_id: str, identity: VerifiedIdentity | None = None) -> dict[str, Any]:
        if identity is None:
            return {"status": "verification_required"}
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 10 * 1024 * 1024:
                return {"status": "unavailable"}
            records = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                return {"status": "unavailable"}
            for record in records:
                if not isinstance(record, dict):
                    continue
                # Do not reveal whether an order exists for a different customer.
                if str(record.get("order_id")) == order_id and str(record.get("customer_id")) == identity.customer_id and str(record.get("email", "")).casefold() == identity.email.casefold():
                    return {"status": "found", "order": _public_order(record)}
            return {"status": "not_found"}
        except (OSError, ValueError, TypeError):
            return {"status": "unavailable"}


class HttpOrderConnector(OrderConnector):
    """A configured read-only gateway, not unrestricted model-selected HTTP/MCP.

The gateway must check BOTH customer_id and order_id. Authentication is configured
by the operator; the model never chooses URLs, credentials, or a write action.
"""

    def __init__(self, url: str, token: str, timeout: float = 8.0):
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Order gateway must be an HTTP(S) URL without embedded credentials")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote order gateways require HTTPS")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def lookup(self, order_id: str, identity: VerifiedIdentity | None = None) -> dict[str, Any]:
        if identity is None:
            return {"status": "verification_required"}
        if not order_id or len(order_id) > 100 or any(ord(char) < 32 for char in order_id):
            return {"status": "not_found"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.get(self.url, params={"order_id": order_id, "customer_id": identity.customer_id}, headers={"Authorization": f"Bearer {self.token}"})
            if response.status_code == 404:
                return {"status": "not_found"}
            response.raise_for_status()
            if len(response.content) > 100_000:
                return {"status": "unavailable"}
            payload = response.json()
            if not isinstance(payload, dict):
                return {"status": "unavailable"}
            if str(payload.get("customer_id")) != identity.customer_id or str(payload.get("email", "")).casefold() != identity.email.casefold() or str(payload.get("order_id")) != order_id:
                return {"status": "not_found"}
            return {"status": "found", "order": _public_order(payload)}
        except (httpx.HTTPError, ValueError, TypeError):
            return {"status": "unavailable"}


def _public_order(record: dict[str, Any]) -> dict[str, Any]:
    # No payment details, addresses, email, notes, or customer identifiers leave
    # the connector. Tracking is text, never a model-selected executable URL.
    allowed = ("order_id", "fulfillment_status", "ordered_at", "dispatched_at", "estimated_delivery", "tracking_number", "carrier", "updated_at")
    return {key: str(record[key])[:300] for key in allowed if record.get(key) is not None}
