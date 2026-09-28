"""Shopify Agentic Storefront client (UCP MCP endpoint: https://{shop}/api/ucp/mcp).

Used for the parts only Shopify can answer: live price, live availability and the checkout
handoff. Product attributes (room/style/material) come from the Bloomreach catalog export
bundled as catalog.json, whose item_id == Shopify SKU.
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx


class ShopifyError(RuntimeError):
    pass


class ShopifyStorefront:
    # The UCP endpoint rate-limits (HTTP 429). Price and stock don't move within a demo, so variant
    # lookups are cached briefly and a 429 waits for Retry-After (capped) before one retry.
    CACHE_TTL_S = 60.0
    MAX_RETRY_WAIT_S = 2.0

    def __init__(self, shop_domain: str, agent_profile: str, timeout_s: float = 8.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.shop = shop_domain
        self._url = f"https://{shop_domain}/api/ucp/mcp"
        self._profile = agent_profile
        self._http = httpx.Client(timeout=timeout_s, transport=transport)
        self._id = 0
        self._cache: dict[str, tuple[float, dict]] = {}

    def _call(self, tool: str, catalog_args: dict) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                "params": {"name": tool, "arguments": {
                    "meta": {"ucp-agent": {"profile": self._profile}}, "catalog": catalog_args}}}
        r = self._http.post(self._url, json=body)
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("retry-after", 1))
            except ValueError:
                wait = 1.0
            time.sleep(min(max(wait, 0.0), self.MAX_RETRY_WAIT_S))
            r = self._http.post(self._url, json=body)
        if r.status_code != 200:
            raise ShopifyError(f"{tool}: HTTP {r.status_code} {r.text[:200]}")
        payload = r.json()
        if "error" in payload:
            raise ShopifyError(f"{tool}: {payload['error']}")
        result = payload["result"]
        if result.get("isError"):
            raise ShopifyError(f"{tool}: {result.get('content')}")
        return json.loads(result["content"][0]["text"])

    def lookup_variants(self, variant_ids: list[str]) -> dict[str, dict]:
        """variant numeric id -> {sku, title, price, available, checkout_url}"""
        now = time.monotonic()
        out = {v: hit[1] for v in variant_ids if (hit := self._cache.get(v)) and now - hit[0] < self.CACHE_TTL_S}
        missing = [v for v in variant_ids if v not in out]
        if not missing:
            return out
        data = self._call("lookup_catalog", {"ids": [f"gid://shopify/ProductVariant/{v}" for v in missing]})
        for p in data.get("products", []):
            for v in p.get("variants", []):
                vid = v["id"].rsplit("/", 1)[-1]
                out[vid] = {
                    "sku": v.get("sku"),
                    "title": p.get("title"),
                    "price": round(v["price"]["amount"] / 100, 2),
                    "currency": v["price"]["currency"],
                    "available": bool(v.get("availability", {}).get("available")),
                    "checkout_url": v.get("checkout_url"),
                }
                self._cache[vid] = (now, out[vid])
        return out

    def search(self, query: str, limit: int = 10) -> list[dict]:
        data = self._call("search_catalog", {"query": query, "pagination": {"limit": limit}})
        rows = []
        for p in data.get("products", []):
            v = p["variants"][0]
            rows.append({"sku": v.get("sku"), "variant_id": v["id"].rsplit("/", 1)[-1], "title": p["title"],
                         "price": round(v["price"]["amount"] / 100, 2),
                         "available": bool(v.get("availability", {}).get("available"))})
        return rows

    def checkout_url(self, lines: list[tuple[str, int]], email: str | None = None,
                     discount: str | None = None, attributes: dict | None = None) -> str:
        """Checkout link for exactly these lines.

        A cart permalink *adds* to whatever is already in the shopper's cart, so a browser that
        abandoned an earlier checkout would carry those items into this one. /cart/clear?return_to=
        empties the cart first, then builds the permalink cart, so the checkout contains only what
        the agent was asked to buy.

        `email` is accepted but not embedded in the URL: `checkout[email]=` is a checkout-step
        parameter, not valid on a /cart/{variant}:{qty} cart-add permalink, and mixing it in here
        broke the whole query string (including `discount=`, which is valid on its own on a cart
        permalink - confirmed directly against the live store). The shopper enters their email at
        Shopify's real checkout step instead; the agent still has it for its own tracking.
        """
        path = ",".join(f"{vid}:{max(1, int(q))}" for vid, q in lines)
        params = []
        if discount:
            params.append(f"discount={quote(discount)}")
        for k, v in (attributes or {}).items():
            params.append(f"attributes[{quote(k)}]={quote(str(v))}")
        permalink = f"/cart/{path}" + (("?" + "&".join(params)) if params else "")
        return f"https://{self.shop}/cart/clear?return_to={quote(permalink, safe='')}"
