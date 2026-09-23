# T6 order webhook (Shopify → Bloomreach)

The loop-closing piece of the Full Journey Orchestrator. When a checkout completes, Shopify calls
this Cloud Run service, which forwards the order to Bloomreach as:

| Bloomreach command | What it drives |
|---|---|
| `customers` (email, name, city) | Personalization in the nurture emails |
| `purchase` event (`purchase_status=success`) | Triggers the **T6 Post-purchase nurture** scenario |
| `purchase_item` event per line | Trains the **T6 Personalized Picks** recommendation engine |

Property names match the `purchase` / `purchase_item` schema already defined in the
`chipper-tambourine` project, so no extra Bloomreach mapping is needed.

## Reliability behaviour

- **HMAC check** on `X-Shopify-Hmac-Sha256`; bad signature → `401`, never processed.
- **Idempotent**: Firestore key `order_id:topic:financial_status` is claimed before calling
  Bloomreach and marked `done` only after success. Shopify redeliveries return `200` without
  re-sending. A crashed attempt's claim expires after 60 s so Shopify's retry goes through.
- **Bounded retries** to Bloomreach (3 attempts, honours `Retry-After`, fits Shopify's 5 s
  budget). Still failing → `502`, and Shopify retries (up to 8 times over ~4 h).
- **Refund/cancel** after payment is forwarded as a new `purchase` with `returned` / `cancelled`.
- **Monitoring**: JSON logs (`bloomreach_track_ok`, `bloomreach_track_failed`, `hmac_rejected`,
  `order_duplicate`) + a log-based metric `t6_bloomreach_track_failed` with an email alert. This
  catches the "order completed but the Bloomreach touch silently failed" demo failure.
- `min-instances=1` so the first demo order doesn't wait for a cold start.

## Deploy

1. **Bloomreach API key** (you create this in the UI; it is a credential):
   Workspace settings → Access management → API → create a *private* key with permission to
   track **events** and update **customer properties**. Copy the key ID and secret.
2. **Shopify webhook secret**: in the Shopify admin (Settings → Notifications → Webhooks) or your
   app config. Copy the signing secret.
3. Deploy (prompts for the three secrets and stores them in Secret Manager, input hidden):
   ```bash
   PROJECT_ID=qwiklabs-gcp-00-8078dcc7f11b ALERT_EMAIL=<you@...> ./scripts/deploy.sh
   ```
4. Point Shopify topics `orders/paid`, `orders/updated`, `orders/cancelled` at
   `https://<service-url>/webhooks/shopify/orders`.

## Demo without a real checkout

```bash
SHOPIFY_WEBHOOK_SECRET=<same secret> python scripts/send_test_order.py https://<service-url> you@example.com
```
Then in Bloomreach: the customer profile shows a `purchase` event, and (once the scenario is
started) the nurture emails arrive 2 minutes apart.

## Test

```bash
pip install -r requirements-dev.txt && pytest -q     # 10 tests: HMAC, transform, dedupe, retries
```

## Things to align with the team

- **Product IDs:** Shopify products are imported with `Variant SKU` = Bloomreach catalog
  `item_id` (see `shopify_products_import.csv`). `transform.py` uses the line item's SKU as
  `product_id` (falls back to Shopify's numeric id, which is also kept as `shopify_product_id`).
- **Databricks write-back (built)**: every paid order is MERGEd into `databricks-hackathon`.fourcast.orders
  (idempotent on order_id, carries the agent's t6_session as conversation_id) when DATABRICKS_HOST,
  DATABRICKS_TOKEN and DATABRICKS_WAREHOUSE_ID are set; see ../enable_databricks.sh. Best effort:
  a Lakehouse error is logged (`databricks_write_failed`) and never fails the Shopify webhook.
- Older note: the write-back could instead hang off the same event: publish
  the order to Pub/Sub after `bloomreach_track_ok` and let a Databricks job consume it, rather
  than adding a second synchronous call inside Shopify's 5 s window.
