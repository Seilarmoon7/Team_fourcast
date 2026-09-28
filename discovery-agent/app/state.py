"""Journey state: everything the agent needs to resume a conversation on any Cloud Run instance.

Backends: memory (tests), firestore (default today), databricks (Lakebase / Delta table, once
the workspace is provisioned: same interface, swap with STATE_BACKEND=databricks).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Protocol


@dataclass
class Journey:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    email: str | None = None
    stage: str = "discovery"            # discovery -> recommending -> checkout -> purchased
    prefs: dict = field(default_factory=dict)       # room, style, budget_max, colors_avoid, pieces
    shown: list = field(default_factory=list)       # SKUs already recommended
    checkout_url: str | None = None
    campaign_requested: str | None = None     # goal handed to Bloomreach, one per journey
    persona: str | None = None                # demo shopper key (see app/personas.json)
    profile_cache: dict | None = None         # Bloomreach/Databricks features.get() result, fetched once per journey
    history: list = field(default_factory=list)     # Gemini Content dicts (JSON-safe)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Journey":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class JourneyStore(Protocol):
    def load(self, session_id: str) -> Journey | None: ...
    def save(self, j: Journey) -> None: ...


class MemoryJourneyStore:
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def load(self, session_id: str) -> Journey | None:
        d = self._d.get(session_id)
        return Journey.from_dict(d) if d else None

    def save(self, j: Journey) -> None:
        j.updated_at = time.time()
        self._d[j.session_id] = j.to_dict()


class FirestoreJourneyStore:
    MAX_HISTORY = 60   # keep documents well under Firestore's 1 MiB limit

    def __init__(self, collection: str) -> None:
        from google.cloud import firestore

        self._col = firestore.Client().collection(collection)

    def load(self, session_id: str) -> Journey | None:
        snap = self._col.document(session_id).get()
        return Journey.from_dict(snap.to_dict()) if snap.exists else None

    def save(self, j: Journey) -> None:
        j.updated_at = time.time()
        j.history = j.history[-self.MAX_HISTORY:]
        self._col.document(j.session_id).set(j.to_dict())
