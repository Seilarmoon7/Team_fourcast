"""Shopify order payload -> Bloomreach Tracking API batch commands.

Property names follow the `purchase` / `purchase_item` event schema that already exists in the
chipper-tambourine project (purchase_id, purchase_status, product_list, total_price, ...), so the
project's standard mapping, the recommendation engine and the post-purchase scenario all pick
these events up without extra configuration.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _ts(iso: str | None) -> float:
    if not iso:
        return datetime.now().timestamp()
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _f(v: Any) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def customer_ids(order: dict) -> dict:
    cust = order.get("customer") or {}
    ids: dict[str, str] = {}
    if cust.get("id"):
        ids["shopify_id"] = str(cust["id"])
    email = (order.get("email") or cust.get("email") or "").strip().lower()
    if email:
        ids["email_id"] = email
    if not ids:
        raise ValueError("order has no customer id or email; cannot attribute purchase")
    return ids


def purchase_status(order: dict) -> str:
    if order.get("cancelled_at"):
        return "cancelled"
    fin = (order.get("financial_status") or "").lower()
    if fin in {"refunded"}:
        return "returned"
    if fin in {"paid", "partially_paid", "authorized", "partially_refunded"}:
        return "success"
    return "created"


def accepts_marketing(order: dict) -> bool:
    cust = order.get("customer") or {}
    consent = (cust.get("email_marketing_consent") or {}).get("state")
    return bool(order.get("buyer_accepts_marketing")) or consent == "subscribed"


def note_attr(order: dict, name: str) -> str | None:
    for a in order.get("note_attributes") or []:
        if a.get("name") == name:
            return str(a.get("value"))
    return None


def build_commands(order: dict) -> list[dict]:
    ids = customer_ids(order)
    ts = _ts(order.get("processed_at") or order.get("created_at"))
    purchase_id = str(order.get("name") or order.get("id"))
    status = purchase_status(order)
    currency = order.get("currency") or "USD"
    ship = order.get("shipping_address") or {}
    lines = order.get("line_items") or []

    product_list = [
        {
            # SKU == Bloomreach catalog item_id (see catalog/build_shopify_csv.py); fall back to Shopify ids
            "product_id": str(li.get("sku") or li.get("product_id") or li.get("id")),
            "shopify_product_id": str(li.get("product_id") or ""),
            "variant_id": str(li.get("variant_id") or ""),
            "title": li.get("title") or li.get("name"),
            "quantity": int(li.get("quantity") or 1),
            "price": _f(li.get("price")),
        }
        for li in lines
    ]

    commands: list[dict] = []

    # 1) keep the profile fresh (email + name) so personalization in emails works
    cust = order.get("customer") or {}
    props = {k: v for k, v in {
        "email": (order.get("email") or cust.get("email") or "").lower() or None,
        "first_name": cust.get("first_name"),
        "last_name": cust.get("last_name"),
        "city": ship.get("city"),
        "country": ship.get("country"),
    }.items() if v}
    if props:
        commands.append({"name": "customers", "data": {"customer_ids": ids, "properties": props}})

    # 1b) buyer ticked "email me with news and offers" at checkout -> email consent in Bloomreach,
    #     so the nurture scenario is allowed to email them (category "other" = the one it uses)
    if accepts_marketing(order):
        commands.append({"name": "customers/events", "data": {
            "customer_ids": ids, "event_type": "consent", "timestamp": ts - 0.001,
            "properties": {"action": "accept", "category": "other", "valid_until": "unlimited",
                           "source": "shopify_checkout"}}})

    # 2) one `purchase` event per order (this is what triggers the nurture scenario)
    commands.append({
        "name": "customers/events",
        "data": {
            "customer_ids": ids,
            "event_type": "purchase",
            "timestamp": ts,
            "properties": {
                "purchase_id": purchase_id,
                "purchase_status": status,
                "purchase_source_type": "online",
                "purchase_source_name": "shopify_agentic_storefront",
                "currency": currency,
                "local_currency": order.get("presentment_currency") or currency,
                "product_list": product_list,
                "product_ids": [p["product_id"] for p in product_list],
                "total_quantity": sum(p["quantity"] for p in product_list),
                "total_price": _f(order.get("total_price")),
                "total_price_without_tax": _f(order.get("subtotal_price")),
                "tax_value": _f(order.get("total_tax")),
                "shipping_city": ship.get("city"),
                "shipping_country": ship.get("country"),
                "shipping_region": ship.get("province"),
                "shipping_zip": ship.get("zip"),
                "payment_type": ",".join(order.get("payment_gateway_names") or []),
                "_discount_code_list": [d.get("code") for d in order.get("discount_codes") or []],
                "_financial_status": order.get("financial_status"),
                # set by the discovery agent's checkout link -> joins the conversation to the order
                "journey_session": note_attr(order, "t6_session"),
            },
        },
    })

    # 3) one `purchase_item` event per line (feeds the recommendation engine)
    for p in product_list:
        commands.append({
            "name": "customers/events",
            "data": {
                "customer_ids": ids,
                "event_type": "purchase_item",
                "timestamp": ts,
                "properties": {
                    "purchase_id": purchase_id,
                    "purchase_status": status,
                    "purchase_source_type": "online",
                    "product_id": p["product_id"],
                    "variant_id": p["variant_id"],
                    "title": p["title"],
                    "quantity": p["quantity"],
                    "price": p["price"],
                    "total_price": round(p["price"] * p["quantity"], 2),
                    "currency": currency,
                },
            },
        })
    return commands
