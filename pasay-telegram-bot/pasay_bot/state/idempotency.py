"""Write-operation idempotency (design §8).

States: ``in_flight`` -> ``done`` (store result) | ``failed`` (allow retry).
``done`` replays the stored result without touching the API; ``in_flight``
blocks concurrent duplicate clicks.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from pasay_bot.state.store import DEFAULT_IN_FLIGHT_TTL, StateStore


class IdempotencyGuard:
    def __init__(self, store: StateStore):
        self.store = store

    @staticmethod
    def _is_stale_in_flight(row: dict) -> bool:
        try:
            created = int(row["created_at"])
        except (TypeError, ValueError):
            return False
        return int(time.time()) - created > DEFAULT_IN_FLIGHT_TTL

    def acquire(self, key: str, kind: str = "", resource: str = "") -> str:
        """Returns one of: 'done' (replay), 'in_flight' (block),
        'retry' (previous failure, proceed), 'new' (proceed)."""
        inserted = self.store.insert_idempotency_if_absent(key, kind, resource, "in_flight")
        if inserted:
            return "new"
        row = self.store.get_idempotency(key)
        if row is None:
            # Expired between insert and read; start over.
            self.store.insert_idempotency_if_absent(key, kind, resource, "in_flight")
            return "new"
        if row["status"] == "done":
            return "done"
        if row["status"] == "in_flight":
            if self._is_stale_in_flight(row):
                # A previous attempt died mid-write (crash/restart). Treat it
                # as failed so the retry can proceed (F3); the stored resource
                # is kept so a landed write can be resumed instead of repeated.
                self.store.update_idempotency(key, "failed", resource=resource)
                return "retry"
            return "in_flight"
        if row["status"] == "failed":
            self.store.update_idempotency(key, "in_flight", resource=resource)
            return "retry"
        return "new"

    def settle(self, key: str, result: Any, resource: Optional[str] = None) -> None:
        self.store.update_idempotency(key, "done", resource=resource, result=result)

    def fail(self, key: str, resource: Optional[str] = None) -> None:
        self.store.update_idempotency(key, "failed", resource=resource)

    def result(self, key: str) -> Optional[Any]:
        row = self.store.get_idempotency(key)
        if row is None:
            return None
        return row["result"]

    def resource(self, key: str) -> str:
        row = self.store.get_idempotency(key)
        if row is None:
            return ""
        return row["resource"] or ""
