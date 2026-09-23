"""Send a correctly signed fake Shopify `orders/paid` webhook to the service.

Lets you demo the whole loop (order -> Bloomreach purchase event -> nurture scenario) without a
real checkout. Usage:
  SHOPIFY_WEBHOOK_SECRET=... python scripts/send_test_order.py https://<service-url> [email]
Each run uses a fresh order id so it is not deduplicated.
"""
import base64, hashlib, hmac, json, os, random, sys
from pathlib import Path

import httpx

url = sys.argv[1].rstrip("/") + "/webhooks/shopify/orders"
order = json.loads((Path(__file__).parent / "sample_order.json").read_text())
order["id"] = random.randint(10**17, 10**18)
order["name"] = f"#T6-{random.randint(1000, 9999)}"
if len(sys.argv) > 2:
    order["email"] = order["customer"]["email"] = sys.argv[2]
body = json.dumps(order).encode()
sig = base64.b64encode(hmac.new(os.environ["SHOPIFY_WEBHOOK_SECRET"].encode(), body, hashlib.sha256).digest()).decode()
r = httpx.post(url, content=body, timeout=10, headers={
    "Content-Type": "application/json", "X-Shopify-Topic": "orders/paid",
    "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Webhook-Id": f"test-{order['id']}",
    "X-Shopify-Shop-Domain": "t6-demo.myshopify.com"})
print(r.status_code, order["name"], r.text[:200])
