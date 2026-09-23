"""Shopify order webhook -> Bloomreach `purchase` + `purchase_item` events.

This is the loop-closing seam of the T6 Full Journey Orchestrator: when the Agentic Storefront
checkout completes, Shopify calls this service, which forwards the order into Bloomreach where it
(a) triggers the "T6 Post-purchase nurture" scenario and (b) feeds the "T6 Personalized Picks"
recommendation engine.

Contract with Shopify:
  * 401 on bad HMAC (never processed)
  * 200 on success or on a duplicate delivery (idempotent)
  * 409 while another instance is still processing the same order (Shopify will retry)
  * 5xx on Bloomreach failure -> Shopify retries (up to 8 times over ~4h)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sys
import time
from functools import lru_cache

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .bloomreach import BloomreachClient, BloomreachError
from .config import Settings, load_settings
from .dedupe import DedupeStore, FirestoreStore, MemoryStore
from .transform import build_commands, purchase_status

HANDLED_TOPICS = {"orders/paid", "orders/create", "orders/cancelled", "orders/updated"}


# ---- structured logging (Cloud Logging parses JSON lines on stdout) ---------------------------
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
log = logging.getLogger("order-webhook")
log.handlers = [_handler]
log.setLevel(logging.INFO)
log.propagate = False


def _log(level: int, event: str, **fields) -> None:
    log.log(level, event, extra={"fields": {"event": event, **fields}})


# ---- dependencies (overridable in tests) --------------------------------------------------------
@lru_cache
def get_settings() -> Settings:
    return load_settings()


@lru_cache
def get_store() -> DedupeStore:
    s = get_settings()
    return MemoryStore() if s.dedupe_backend == "memory" else FirestoreStore(s.dedupe_collection)


@lru_cache
def get_bloomreach() -> BloomreachClient:
    s = get_settings()
    return BloomreachClient(s.bloomreach_base_url, s.bloomreach_project_token,
                            s.bloomreach_api_key_id, s.bloomreach_api_secret,
                            timeout_s=s.request_timeout_s, max_attempts=s.max_attempts)


@lru_cache
def get_databricks():
    s = get_settings()
    if not (s.databricks_host and s.databricks_token and s.databricks_warehouse_id):
        return None
    from .databricks import DatabricksOrders
    return DatabricksOrders(s.databricks_host, s.databricks_token, s.databricks_warehouse_id,
                            s.databricks_orders_table)


def verify_hmac(raw: bytes, header_value: str | None, secret: str) -> bool:
    if not header_value:
        return False
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header_value)


app = FastAPI(title="t6-order-webhook")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/webhooks/shopify/orders")
async def shopify_orders(
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_topic: str | None = Header(default=None),
    x_shopify_webhook_id: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
) -> Response:
    started = time.monotonic()
    raw = await request.body()
    settings = get_settings()

    if not verify_hmac(raw, x_shopify_hmac_sha256, settings.shopify_webhook_secret):
        _log(logging.WARNING, "hmac_rejected", shop=x_shopify_shop_domain, webhook_id=x_shopify_webhook_id)
        raise HTTPException(status_code=401, detail="invalid signature")

    topic = x_shopify_topic or "orders/paid"
    if topic not in HANDLED_TOPICS:
        _log(logging.INFO, "topic_ignored", topic=topic)
        return Response(status_code=200)

    order = json.loads(raw)
    order_id = str(order.get("id"))
    # Key on the *Bloomreach* purchase_status, not the Shopify topic or financial_status: Shopify
    # sends several webhooks for one payment (orders/paid + orders/updated, authorized then paid)
    # and they all mean "success", so keying on the raw values fired the nurture journey twice.
    # A later refund or cancellation maps to a different status and is still forwarded.
    key = f"{order_id}:{purchase_status(order)}"
    store = get_store()

    claim = store.claim(key)
    if claim == "done":
        _log(logging.INFO, "order_duplicate", order_id=order_id, topic=topic, webhook_id=x_shopify_webhook_id)
        return Response(status_code=200)
    if claim == "in_progress":
        _log(logging.INFO, "order_in_progress", order_id=order_id, topic=topic)
        raise HTTPException(status_code=409, detail="already processing")

    try:
        commands = build_commands(order)
        get_bloomreach().send_batch(commands)
    except (BloomreachError, ValueError) as e:
        store.release(key)
        _log(logging.ERROR, "bloomreach_track_failed", order_id=order_id, topic=topic,
             error=str(e), latency_ms=int((time.monotonic() - started) * 1000))
        raise HTTPException(status_code=502, detail="upstream tracking failed") from e

    store.mark_done(key)
    # Lakehouse write-back is best effort: Bloomreach (the customer-facing journey) already succeeded,
    # and failing here would make Shopify retry an order we have already forwarded.
    dbx = get_databricks()
    if dbx is not None:
        try:
            dbx.upsert(order)
            _log(logging.INFO, "databricks_write_ok", order_id=order_id)
        except Exception as e:
            _log(logging.ERROR, "databricks_write_failed", order_id=order_id, error=str(e)[:300])
    _log(logging.INFO, "bloomreach_track_ok", order_id=order_id, topic=topic,
         purchase_id=order.get("name"), commands=len(commands),
         total_price=order.get("total_price"), latency_ms=int((time.monotonic() - started) * 1000))
    return Response(status_code=200)
