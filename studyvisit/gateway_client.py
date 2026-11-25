"""HTTP client for the local fixture gateway / counterparty receipt stub.

It implements the protected retry protocol from the README:

* send a stable ``reference`` with ``POST /v1/receipts``;
* if the stub is in ``lost_reply`` mode the connection is dropped after the
  server has recorded the reference, so recover with
  ``GET /v1/receipts/<reference>`` (or one idempotent retry);
* reusing a reference for a different operation is a 409.
"""

from __future__ import annotations

import json
import time
from http.client import HTTPConnection, RemoteDisconnected
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .errors import ReceiptUnavailable, ReferenceReused


class GatewayClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", *, timeout: float = 2.0):
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.timeout = timeout

    def _conn(self) -> HTTPConnection:
        return HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _json_request(self, method: str, path: str, payload: Optional[dict] = None):
        conn = self._conn()
        try:
            body = None if payload is None else json.dumps(payload)
            conn.request(method, path, body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
            parsed = json.loads(data) if data else None
            return resp.status, parsed
        finally:
            conn.close()

    # ------------------------------------------------------------- fixtures API
    def registers(self) -> Dict[str, Any]:
        status, body = self._json_request("GET", "/v1/registers")
        if status != 200:
            raise ReceiptUnavailable(f"registers fetch failed: {status}")
        return body

    def events_page(self, after: int = 0, limit: int = 50) -> Dict[str, Any]:
        status, body = self._json_request("GET", f"/v1/events?after={after}&limit={limit}")
        if status != 200:
            raise ReceiptUnavailable(f"events fetch failed: {status}")
        return body

    def all_events(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor = 0
        while True:
            page = self.events_page(after=cursor)
            events = page.get("events", [])
            out.extend(events)
            nxt = page.get("next_cursor", cursor)
            if not events or nxt == cursor:
                break
            cursor = nxt
        return out

    # -------------------------------------------------------------- receipts
    def get_receipt(self, reference: str) -> Optional[Dict[str, Any]]:
        try:
            status, body = self._json_request("GET", f"/v1/receipts/{reference}")
        except (RemoteDisconnected, ConnectionError, OSError):
            return None
        if status == 404:
            return None
        if status != 200:
            raise ReceiptUnavailable(f"receipt lookup failed: {status}")
        return body

    def post_receipt_once(self, reference: str, operation: str, recipient: str):
        """Return ``(status, body)`` or raise RemoteDisconnected on lost reply."""
        return self._json_request(
            "POST",
            "/v1/receipts",
            {"reference": reference, "operation": operation, "recipient": recipient},
        )

    def publish_with_recovery(
        self,
        reference: str,
        operation: str,
        recipient: str,
        *,
        retries: int = 2,
        backoff: float = 0.05,
    ) -> Dict[str, Any]:
        """Publish and always recover the recorded receipt if at all possible."""
        lost = False
        try:
            status, body = self.post_receipt_once(reference, operation, recipient)
            if status == 409:
                raise ReferenceReused(body.get("code") if body else "reference_reused", details=body)
            if status == 200 and body:
                return {"reference": reference, "receipt": body.get("receipt"), "recovery": "direct", "lost_reply": False}
        except RemoteDisconnected:
            lost = True  # server recorded it but closed before responding
        except (ConnectionError, OSError):
            lost = True

        # Recover via lookup, then idempotent same-reference retry.
        for attempt in range(retries + 1):
            got = self.get_receipt(reference)
            if got and got.get("receipt") is not None:
                return {"reference": reference, "receipt": got["receipt"],
                        "recovery": "lookup_after_lost_reply" if lost else "lookup",
                        "lost_reply": lost}
            try:
                status, body = self.post_receipt_once(reference, operation, recipient)
                if status == 409:
                    raise ReferenceReused("reference_reused", details=body)
                if status == 200 and body:
                    return {"reference": reference, "receipt": body.get("receipt"),
                            "recovery": "idempotent_retry", "lost_reply": lost}
            except RemoteDisconnected:
                lost = True
            except (ConnectionError, OSError):
                pass
            if attempt < retries:
                time.sleep(backoff)
        raise ReceiptUnavailable("receipt could not be recovered", details={"reference": reference})
