"""Bloomreach Engagement client: tracking (events + profile) and customer attributes
(properties + the "T6 Personalized Picks" recommendation)."""
from __future__ import annotations

import time

import httpx


class BloomreachError(RuntimeError):
    pass


class Bloomreach:
    def __init__(self, base_url: str, project_token: str, key_id: str, secret: str,
                 recommendation_id: str, timeout_s: float = 4.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        base = base_url.rstrip("/")
        self._track = f"{base}/track/v2/projects/{project_token}/batch"
        self._attrs = f"{base}/data/v2/projects/{project_token}/customers/attributes"
        self._http = httpx.Client(auth=(key_id, secret), timeout=timeout_s, transport=transport)
        self.recommendation_id = recommendation_id
        self.enabled = bool(project_token and key_id and secret)

    # ---- tracking ------------------------------------------------------------------------------
    def track(self, email: str, events: list[tuple[str, dict]], properties: dict | None = None) -> None:
        if not self.enabled or not email:
            return
        ids = {"email_id": email.lower()}
        now = time.time()
        cmds: list[dict] = []
        if properties:
            cmds.append({"name": "customers", "data": {"customer_ids": ids, "properties": properties}})
        for i, (etype, props) in enumerate(events):
            cmds.append({"name": "customers/events", "data": {
                "customer_ids": ids, "event_type": etype, "timestamp": now + i * 0.001, "properties": props}})
        if not cmds:
            return
        r = self._http.post(self._track, json={"commands": cmds})
        if r.status_code != 200:
            raise BloomreachError(f"track HTTP {r.status_code}: {r.text[:200]}")
        bad = [x for x in r.json().get("results", []) if not x.get("success")]
        if bad:
            raise BloomreachError(f"track rejected: {bad[:2]}")

    # ---- reads ---------------------------------------------------------------------------------
    def customer_attributes(self, email: str, properties: list[str], rec_size: int = 6) -> dict:
        """Returns {"properties": {...}, "recommendations": [item_id, ...]}"""
        if not self.enabled or not email:
            return {"properties": {}, "recommendations": []}
        attrs = [{"type": "property", "property": p} for p in properties]
        attrs.append({"type": "recommendation", "id": self.recommendation_id, "size": rec_size,
                      "fillWithRandom": True, "catalogAttributesWhitelist": ["title"]})
        r = self._http.post(self._attrs, json={"customer_ids": {"email_id": email.lower()}, "attributes": attrs})
        if r.status_code != 200:
            raise BloomreachError(f"attributes HTTP {r.status_code}: {r.text[:200]}")
        results = r.json().get("results", [])
        props = {p: (res.get("value") if res.get("success") else None) for p, res in zip(properties, results)}
        rec_res = results[len(properties)] if len(results) > len(properties) else {}
        recs = []
        if rec_res.get("success"):
            for it in rec_res.get("value") or []:
                iid = it.get("item_id") or it.get("product_id") if isinstance(it, dict) else it
                if iid:
                    recs.append(str(iid))
        return {"properties": props, "recommendations": recs}
