"""Idempotency store keyed by Shopify order id + topic.

Shopify delivers webhooks at-least-once and retries on any non-2xx, so the same order can arrive
several times. We claim the key *before* calling Bloomreach and only mark it `done` after success.
A claim older than CLAIM_TTL_S is treated as abandoned (the previous attempt crashed) and can be
re-claimed, so a failed attempt never blocks Shopify's retry forever.
"""
from __future__ import annotations

import time
from typing import Protocol

CLAIM_TTL_S = 60


class DedupeStore(Protocol):
    def claim(self, key: str) -> str: ...          # "claimed" | "done" | "in_progress"
    def mark_done(self, key: str) -> None: ...
    def release(self, key: str) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self._d: dict[str, tuple[str, float]] = {}

    def claim(self, key: str) -> str:
        now = time.time()
        state = self._d.get(key)
        if state:
            status, at = state
            if status == "done":
                return "done"
            if now - at < CLAIM_TTL_S:
                return "in_progress"
        self._d[key] = ("processing", now)
        return "claimed"

    def mark_done(self, key: str) -> None:
        self._d[key] = ("done", time.time())

    def release(self, key: str) -> None:
        self._d.pop(key, None)


class FirestoreStore:
    def __init__(self, collection: str) -> None:
        from google.cloud import firestore  # imported lazily so tests don't need GCP creds

        self._fs = firestore
        self._db = firestore.Client()
        self._col = self._db.collection(collection)

    def claim(self, key: str) -> str:
        ref = self._col.document(key)
        fs = self._fs

        @fs.transactional
        def _txn(tx) -> str:
            snap = ref.get(transaction=tx)
            now = time.time()
            if snap.exists:
                d = snap.to_dict()
                if d.get("status") == "done":
                    return "done"
                if now - d.get("claimed_at", 0) < CLAIM_TTL_S:
                    return "in_progress"
            tx.set(ref, {"status": "processing", "claimed_at": now})
            return "claimed"

        return _txn(self._db.transaction())

    def mark_done(self, key: str) -> None:
        self._col.document(key).set({"status": "done", "done_at": time.time()}, merge=True)

    def release(self, key: str) -> None:
        self._col.document(key).delete()
