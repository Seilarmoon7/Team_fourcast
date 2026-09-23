"""HTTP surface for the discovery agent (Cloud Run)."""
from __future__ import annotations

import json
import logging
import sys
import time
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .agent import DiscoveryAgent
from .bloomreach import Bloomreach
from .config import Settings, load_settings
from .features import BloomreachFeatures, DatabricksFeatures
from .shopify import ShopifyStorefront
from .state import FirestoreJourneyStore, Journey, JourneyStore, MemoryJourneyStore


class _Json(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        return json.dumps({"severity": r.levelname, "message": r.getMessage(), **getattr(r, "fields", {})},
                          default=str)


_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(_Json())
log = logging.getLogger("discovery-agent")
log.handlers, log.propagate = [_h], False
log.setLevel(logging.INFO)


@lru_cache
def get_settings() -> Settings:
    return load_settings()


@lru_cache
def get_store() -> JourneyStore:
    s = get_settings()
    return MemoryJourneyStore() if s.state_backend == "memory" else FirestoreJourneyStore(s.state_collection)


@lru_cache
def get_agent() -> DiscoveryAgent:
    from google import genai

    s = get_settings()
    client = genai.Client(vertexai=True, project=s.gcp_project or None, location=s.gcp_location)
    shop = ShopifyStorefront(s.shop_domain, s.ucp_agent_profile)
    br = Bloomreach(s.bloomreach_base_url, s.bloomreach_project_token, s.bloomreach_api_key_id,
                    s.bloomreach_api_secret, s.recommendation_id)
    features = BloomreachFeatures(br)
    if s.features_backend == "databricks" and s.databricks_host:
        features = DatabricksFeatures(s.databricks_host, s.databricks_token, s.databricks_warehouse_id,
                                      s.databricks_features_table, features)
    return DiscoveryAgent(client, s, shop, br, features)


app = FastAPI(title="t6-discovery-agent")
INDEX = (Path(__file__).parent / "static" / "index.html").read_text()


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=200)
    shopper: str | None = Field(default=None, max_length=32)   # demo shopper key, see app/personas.json


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    started = time.monotonic()
    store = get_store()
    j = (store.load(body.session_id) if body.session_id else None) or Journey()
    if body.shopper and not j.history:          # a shopper is fixed for the whole conversation
        from .agent import PERSONAS
        j.persona = body.shopper if body.shopper in PERSONAS else None
    if body.email and not j.email:
        j.email = body.email.strip().lower()
    try:
        out = get_agent().turn(j, body.message)
    except Exception as e:
        log.exception("agent_turn_failed", extra={"fields": {"event": "agent_turn_failed", "session": j.session_id}})
        raise HTTPException(status_code=502, detail="agent unavailable, please retry") from e
    store.save(j)
    log.info("agent_turn", extra={"fields": {
        "event": "agent_turn", "session": j.session_id, "stage": j.stage, "tools": out["trace"],
        "products": [p["sku"] for p in out["products"]], "latency_ms": int((time.monotonic() - started) * 1000)}})
    return {"session_id": j.session_id, **{k: v for k, v in out.items() if k != "trace"}, "tools": out["trace"]}
