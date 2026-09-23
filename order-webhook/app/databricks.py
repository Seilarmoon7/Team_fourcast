"""Write paid Shopify orders back to the Lakehouse (`databricks-hackathon`.fourcast.orders).

Closes the loop: Shopify order -> Bloomreach purchase (journey) AND -> Databricks orders table, which
feeds customer_360 / customer_profile (LTV, churn risk) that the discovery agent reads next visit.
MERGE on order_id keeps it idempotent across Shopify's duplicate webhooks and retries.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from .transform import note_attr


class DatabricksError(RuntimeError):
    pass


def order_row(order: dict) -> dict:
    cust = order.get("customer") or {}
    lines = [{"sku": li.get("sku"), "title": li.get("title") or li.get("name"),
              "quantity": int(li.get("quantity") or 1), "price": str(li.get("price") or "0")}
             for li in order.get("line_items") or []]
    return {
        "order_id": str(order.get("id")),
        "order_number": str(order.get("name") or order.get("order_number") or ""),
        "email": (order.get("email") or cust.get("email") or "").strip().lower() or None,
        "conversation_id": note_attr(order, "t6_session"),
        "financial_status": order.get("financial_status"),
        "total_price": str(order.get("total_price") or "0"),
        "currency": order.get("currency") or "USD",
        "line_items": json.dumps(lines),
        "ordered_at": order.get("processed_at") or order.get("created_at")
        or datetime.now(timezone.utc).isoformat(),
    }


class DatabricksOrders:
    def __init__(self, host: str, token: str, warehouse_id: str, table: str, timeout_s: float = 8.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._url = f"https://{host.removeprefix('https://').rstrip('/')}/api/2.0/sql/statements"
        self._http = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=timeout_s,
                                  transport=transport)
        self._wh, self._table = warehouse_id, table

    def upsert(self, order: dict) -> None:
        r = order_row(order)
        stmt = (
            f"MERGE INTO {self._table} t USING (SELECT :order_id AS order_id, :order_number AS order_number, "
            ":email AS email, :conversation_id AS conversation_id, :financial_status AS financial_status, "
            "CAST(:total_price AS DECIMAL(18,2)) AS total_price, :currency AS currency, :line_items AS line_items, "
            "CAST(:ordered_at AS TIMESTAMP) AS ordered_at, current_timestamp() AS received_at) s "
            "ON t.order_id = s.order_id "
            "WHEN MATCHED THEN UPDATE SET t.financial_status = s.financial_status, t.total_price = s.total_price, "
            "t.received_at = s.received_at "
            "WHEN NOT MATCHED THEN INSERT *")
        params = [{"name": k, "value": v} for k, v in r.items()]
        resp = self._http.post(self._url, json={"warehouse_id": self._wh, "statement": stmt, "parameters": params,
                                                "wait_timeout": "10s", "on_wait_timeout": "CONTINUE"})
        body = resp.json() if resp.content else {}
        state = (body.get("status") or {}).get("state")
        if resp.status_code != 200 or state in ("FAILED", "CANCELED", "CLOSED"):
            raise DatabricksError(f"HTTP {resp.status_code} state={state} "
                                  f"{(body.get('status') or {}).get('error') or body.get('message')}")
