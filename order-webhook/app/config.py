"""Runtime configuration. All secrets come from env vars populated by Secret Manager on Cloud Run."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    shopify_webhook_secret: str
    bloomreach_project_token: str
    bloomreach_api_key_id: str
    bloomreach_api_secret: str
    bloomreach_base_url: str = "https://api-engagement.bloomreach.com"
    dedupe_backend: str = "firestore"          # "firestore" or "memory" (tests / local)
    dedupe_collection: str = "shopify_order_events"
    request_timeout_s: float = 3.0              # Shopify gives us ~5s total
    max_attempts: int = 3
    # Databricks write-back (optional: enabled when host, token and warehouse are all set)
    databricks_host: str = ""
    databricks_token: str = ""
    databricks_warehouse_id: str = ""
    databricks_orders_table: str = "`databricks-hackathon`.fourcast.orders"


def load_settings() -> Settings:
    def req(name: str) -> str:
        v = os.environ.get(name)
        if not v:
            raise RuntimeError(f"missing required env var {name}")
        return v

    return Settings(
        shopify_webhook_secret=req("SHOPIFY_WEBHOOK_SECRET"),
        bloomreach_project_token=req("BLOOMREACH_PROJECT_TOKEN"),
        bloomreach_api_key_id=req("BLOOMREACH_API_KEY_ID"),
        bloomreach_api_secret=req("BLOOMREACH_API_SECRET"),
        bloomreach_base_url=os.environ.get("BLOOMREACH_BASE_URL", "https://api-engagement.bloomreach.com"),
        dedupe_backend=os.environ.get("DEDUPE_BACKEND", "firestore"),
        dedupe_collection=os.environ.get("DEDUPE_COLLECTION", "shopify_order_events"),
        databricks_host=os.environ.get("DATABRICKS_HOST", ""),
        databricks_token=os.environ.get("DATABRICKS_TOKEN", ""),
        databricks_warehouse_id=os.environ.get("DATABRICKS_WAREHOUSE_ID", ""),
        databricks_orders_table=os.environ.get("DATABRICKS_ORDERS_TABLE", "`databricks-hackathon`.fourcast.orders"),
    )
