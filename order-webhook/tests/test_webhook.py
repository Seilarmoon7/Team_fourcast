import base64
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import main
from app.bloomreach import BloomreachClient, BloomreachError
from app.config import Settings
from app.dedupe import MemoryStore
from app.transform import build_commands, purchase_status

SECRET = "shpss_test"
BASE = "https://api-engagement.bloomreach.com"
TOKEN = "proj-token"
BATCH = f"{BASE}/track/v2/projects/{TOKEN}/batch"
ORDER = json.loads((Path(__file__).parent.parent / "scripts" / "sample_order.json").read_text())


def sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


@pytest.fixture
def client(monkeypatch):
    settings = Settings(SECRET, TOKEN, "kid", "ksecret", BASE, dedupe_backend="memory")
    store = MemoryStore()
    br = BloomreachClient(BASE, TOKEN, "kid", "ksecret", sleep=lambda s: None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "get_bloomreach", lambda: br)
    monkeypatch.setattr(main, "get_databricks", lambda: None)
    return TestClient(main.app)


def post(client, order=ORDER, topic="orders/paid", sig=None):
    body = json.dumps(order).encode()
    return client.post("/webhooks/shopify/orders", content=body, headers={
        "X-Shopify-Hmac-Sha256": sig or sign(body), "X-Shopify-Topic": topic,
        "X-Shopify-Webhook-Id": "wh-1", "Content-Type": "application/json"})


# ---- transform ----------------------------------------------------------------------------------
def test_transform_builds_profile_purchase_and_items():
    cmds = build_commands(ORDER)
    assert [c["data"].get("event_type") for c in cmds] == [None, "purchase", "purchase_item", "purchase_item"]
    purchase = cmds[1]["data"]
    assert purchase["customer_ids"] == {"shopify_id": "7001", "email_id": "maya.demo@example.com"}
    p = purchase["properties"]
    assert p["purchase_status"] == "success"
    assert p["total_price"] == 813.95
    assert p["product_ids"] == ["lr-013", "lr-044"]
    assert p["product_list"][0]["title"] == "Arc Walnut Coffee Table"


def test_status_mapping():
    assert purchase_status({"financial_status": "paid"}) == "success"
    assert purchase_status({"financial_status": "pending"}) == "created"
    assert purchase_status({"financial_status": "refunded"}) == "returned"
    assert purchase_status({"financial_status": "paid", "cancelled_at": "2026-09-21T10:00:00Z"}) == "cancelled"


def test_order_without_identity_is_rejected():
    with pytest.raises(ValueError):
        build_commands({"id": 1, "line_items": []})


# ---- endpoint -----------------------------------------------------------------------------------
@respx.mock
def test_happy_path_then_duplicate_is_idempotent(client):
    route = respx.post(BATCH).mock(return_value=httpx.Response(200, json={"results": [{"success": True}] * 4}))
    assert post(client).status_code == 200
    assert post(client).status_code == 200          # Shopify redelivery
    assert route.call_count == 1                    # ...forwarded exactly once


def test_bad_signature_rejected(client):
    assert post(client, sig="nope").status_code == 401


@respx.mock
def test_retries_on_429_then_succeeds(client):
    route = respx.post(BATCH).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"results": [{"success": True}] * 4})])
    assert post(client).status_code == 200
    assert route.call_count == 2


@respx.mock
def test_upstream_failure_returns_5xx_and_allows_shopify_retry(client):
    route = respx.post(BATCH).mock(return_value=httpx.Response(503))
    assert post(client).status_code == 502
    assert route.call_count == 3                    # bounded retries
    route.mock(return_value=httpx.Response(200, json={"results": [{"success": True}] * 4}))
    assert post(client).status_code == 200          # claim was released, retry goes through


@respx.mock
def test_refund_after_paid_is_forwarded(client):
    route = respx.post(BATCH).mock(return_value=httpx.Response(200, json={"results": [{"success": True}] * 4}))
    post(client)
    refunded = dict(ORDER, financial_status="refunded")
    assert post(client, refunded, topic="orders/updated").status_code == 200
    assert route.call_count == 2


@respx.mock
def test_command_level_rejection_is_not_retried():
    respx.post(BATCH).mock(return_value=httpx.Response(200, json={"results": [{"success": False, "errors": ["bad"]}]}))
    br = BloomreachClient(BASE, TOKEN, "k", "s", sleep=lambda s: None)
    with pytest.raises(BloomreachError):
        br.send_batch([{"name": "customers/events", "data": {}}])


def test_ignored_topic(client):
    assert post(client, topic="products/update").status_code == 200


def test_marketing_opt_in_records_consent_and_session():
    order = dict(ORDER, buyer_accepts_marketing=True, note_attributes=[{"name": "t6_session", "value": "abc"}])
    cmds = build_commands(order)
    consent = [c for c in cmds if c["data"].get("event_type") == "consent"]
    assert consent and consent[0]["data"]["properties"]["action"] == "accept"
    purchase = next(c for c in cmds if c["data"].get("event_type") == "purchase")
    assert purchase["data"]["properties"]["journey_session"] == "abc"
    assert not [c for c in build_commands(ORDER) if c["data"].get("event_type") == "consent"]


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


@respx.mock
def test_one_payment_many_webhooks_is_forwarded_once(client):
    """Shopify fires orders/paid and orders/updated (authorized -> paid) for a single payment."""
    route = respx.post(BATCH).mock(return_value=httpx.Response(200, json={"results": [{"success": True}] * 4}))
    assert post(client, dict(ORDER, financial_status="authorized"), topic="orders/updated").status_code == 200
    assert post(client, dict(ORDER, financial_status="paid"), topic="orders/paid").status_code == 200
    assert post(client, dict(ORDER, financial_status="paid"), topic="orders/updated").status_code == 200
    assert route.call_count == 1                      # one purchase event -> the journey runs once
    refunded = dict(ORDER, financial_status="refunded")
    assert post(client, refunded, topic="orders/updated").status_code == 200
    assert route.call_count == 2                      # a genuine status change still gets through


# ---- Databricks write-back ----------------------------------------------------------------------
DBX = "https://dbc-test.cloud.databricks.com/api/2.0/sql/statements"


def _dbx(monkeypatch):
    from app.databricks import DatabricksOrders
    d = DatabricksOrders("dbc-test.cloud.databricks.com", "dapi", "wh1", "`databricks-hackathon`.fourcast.orders")
    monkeypatch.setattr(main, "get_databricks", lambda: d)


def test_paid_order_is_merged_into_databricks_orders(client, monkeypatch):
    _dbx(monkeypatch)
    order = {**ORDER, "note_attributes": [{"name": "t6_session", "value": "sess-123"}]}
    with respx.mock() as m:
        m.post(BATCH).mock(return_value=httpx.Response(200, json={"results": [{"success": True}] * 20}))
        dbx = m.post(DBX).mock(return_value=httpx.Response(200, json={"status": {"state": "SUCCEEDED"}}))
        assert post(client, order).status_code == 200
        assert dbx.call_count == 1
        body = json.loads(dbx.calls[0].request.content)
        params = {p["name"]: p["value"] for p in body["parameters"]}
        assert body["warehouse_id"] == "wh1" and "MERGE INTO `databricks-hackathon`.fourcast.orders" in body["statement"]
        assert params["order_id"] == str(ORDER["id"]) and params["conversation_id"] == "sess-123"
        assert params["email"] == (ORDER.get("email") or ORDER["customer"]["email"]).lower()
        assert json.loads(params["line_items"])[0]["quantity"] >= 1


def test_databricks_failure_does_not_fail_the_webhook(client, monkeypatch):
    _dbx(monkeypatch)
    with respx.mock() as m:
        m.post(BATCH).mock(return_value=httpx.Response(200, json={"results": [{"success": True}] * 20}))
        m.post(DBX).mock(return_value=httpx.Response(200, json={"status": {"state": "FAILED", "error": {"message": "x"}}}))
        assert post(client).status_code == 200          # Bloomreach succeeded; Lakehouse error is only logged
